import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy import select, delete
from app.database.models import Base, User, Envelope, Transaction
from app.bot.handlers.transactions import (
    verify_user_ledger,
    handle_transaction,
    confirm_income,
    IncomeStates
)
from app.services.ai_brain import BrainResponse, PydanticEnvelope, PlanItem, TransactionItem
from unittest.mock import AsyncMock, MagicMock

# In-memory SQLite for testing
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

@pytest_asyncio.fixture
async def test_session_maker():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()

@pytest.fixture
def mock_message():
    message = AsyncMock()
    message.from_user.id = 12345
    message.from_user.username = "test_user"
    message.chat.do = AsyncMock()
    message.answer = AsyncMock()
    return message

@pytest.fixture
def mock_callback():
    callback = AsyncMock()
    callback.from_user.id = 12345
    callback.message = AsyncMock()
    callback.message.edit_text = AsyncMock()
    callback.message.answer = AsyncMock()
    return callback

@pytest.fixture
def mock_state():
    state = AsyncMock()
    state_data = {}
    
    async def set_state(s):
        state.current_state = s
    async def set_data(d):
        nonlocal state_data
        state_data = d
    async def get_data():
        return state_data
    async def get_state():
        return getattr(state, "current_state", None)
    async def clear():
        nonlocal state_data
        state_data = {}
        state.current_state = None

    state.set_state = set_state
    state.set_data = set_data
    state.get_data = get_data
    state.get_state = get_state
    state.clear = clear
    return state

@pytest.mark.asyncio
async def test_profile_update_ledger_onboarding(test_session_maker, mock_message, mock_state, monkeypatch):
    # 1. Setup initial user
    async with test_session_maker() as session:
        user = User(telegram_id=12345, invite_code="CF-12345")
        session.add(user)
        await session.commit()

    # 2. Mock AI response for profile_update
    brain_resp = BrainResponse(
        thoughts="Setting up budget",
        intent="profile_update",
        monthly_income=100000.0,
        free_cash=30000.0,
        plan_items=[
            PlanItem(name="Аренда", amount=10000.0),
            PlanItem(name="Продукты", amount=10000.0)
        ],
        envelopes_to_create=[
            PydanticEnvelope(name="Аренда", target_amount=30000.0, current_amount=0.0, is_debt=False, is_goal=False),
            PydanticEnvelope(name="Продукты", target_amount=15000.0, current_amount=0.0, is_debt=False, is_goal=False)
        ],
        show_dashboard=True,
        coach_reply="Бюджет сформирован."
    )

    async def mock_process(*args, **kwargs):
        return brain_resp

    monkeypatch.setattr("app.bot.handlers.transactions.process_user_message", mock_process)
    # Monkeypatch the session maker inside transactions handler
    monkeypatch.setattr("app.bot.handlers.transactions.async_session_maker", lambda: test_session_maker())

    # 3. Process setup transaction
    await handle_transaction(mock_message, "Привет, у меня 30к свободных, создай аренду на 30к и продукты на 15к", state=mock_state)

    # 4. Assertions on Database State
    async with test_session_maker() as session:
        # Check Envelopes
        envs_res = await session.execute(select(Envelope).where(Envelope.user_id == 12345))
        envs = list(envs_res.scalars().all())
        assert len(envs) == 3  # Аренда, Продукты, Нераспределённые
        
        unallocated = next(e for e in envs if e.name == "Нераспределённые")
        assert unallocated.current_amount == 30000.0
        
        rent = next(e for e in envs if e.name == "Аренда")
        assert rent.current_amount == 0.0
        assert rent.target_amount == 30000.0

        # Check Transactions
        txs_res = await session.execute(select(Transaction).where(Transaction.user_id == 12345))
        txs = list(txs_res.scalars().all())
        assert len(txs) == 1
        assert txs[0].amount == 30000.0
        assert txs[0].envelope_id == unallocated.id
        assert txs[0].description == "Стартовый капитал"

        # Verify ledger
        assert await verify_user_ledger(session, 12345) is True

    # 5. Check FSM state was set to confirming
    assert await mock_state.get_state() == IncomeStates.confirming
    fsm_data = await mock_state.get_data()
    assert fsm_data["income_amount"] == 30000.0
    assert fsm_data["unallocated_env_id"] == unallocated.id
    assert "Аренда" in fsm_data["alloc_names"]
    assert fsm_data["alloc_amounts"] == [10000.0, 10000.0]


@pytest.mark.asyncio
async def test_confirm_income_double_entry(test_session_maker, mock_callback, mock_state, monkeypatch):
    monkeypatch.setattr("app.bot.handlers.transactions.async_session_maker", lambda: test_session_maker())

    # 1. Setup user, envelopes, and starting transaction
    async with test_session_maker() as session:
        user = User(telegram_id=12345, invite_code="CF-12345")
        unallocated = Envelope(user_id=12345, name="Нераспределённые", current_amount=30000.0)
        rent = Envelope(user_id=12345, name="Аренда", current_amount=0.0, target_amount=30000.0)
        session.add_all([user, unallocated, rent])
        await session.flush()
        
        tx = Transaction(user_id=12345, amount=30000.0, envelope_id=unallocated.id, description="Старт")
        session.add(tx)
        await session.commit()
        
        unallocated_id = unallocated.id
        rent_id = rent.id

    # 2. Setup FSM state data
    await mock_state.set_state(IncomeStates.confirming)
    await mock_state.set_data({
        "income_amount": 30000.0,
        "unallocated_env_id": unallocated_id,
        "alloc_names": ["Аренда"],
        "alloc_amounts": [10000.0]
    })

    # 3. Call confirm_income callback
    await confirm_income(mock_callback, mock_state)

    # 4. Check balances and ledger transactions
    async with test_session_maker() as session:
        envs_res = await session.execute(select(Envelope).where(Envelope.user_id == 12345))
        envs = list(envs_res.scalars().all())
        unallocated_db = next(e for e in envs if e.name == "Нераспределённые")
        rent_db = next(e for e in envs if e.name == "Аренда")
        
        # Balances checked: Rent got 10k, Unallocated decreased to 20k
        assert rent_db.current_amount == 10000.0
        assert unallocated_db.current_amount == 20000.0

        # Check transactions
        txs_res = await session.execute(select(Transaction).where(Transaction.user_id == 12345))
        txs = list(txs_res.scalars().all())
        # We expect: 1 initial (30k) + 1 negative on unallocated (-10k) + 1 positive on rent (10k) = 3 transactions
        assert len(txs) == 3
        
        tx_unallocated = next(t for t in txs if t.envelope_id == unallocated_db.id and t.amount == -10000.0)
        assert tx_unallocated.description == f"Распределение: {rent_db.name}"

        tx_rent = next(t for t in txs if t.envelope_id == rent_db.id and t.amount == 10000.0)
        assert tx_rent.description == "Распределение дохода: Аренда"

        # Verify ledger
        assert await verify_user_ledger(session, 12345) is True


@pytest.mark.asyncio
async def test_overdraft_coverage_double_entry(test_session_maker, mock_message, mock_state, monkeypatch):
    monkeypatch.setattr("app.bot.handlers.transactions.async_session_maker", lambda: test_session_maker())

    # 1. Setup user, envelopes, transactions
    async with test_session_maker() as session:
        user = User(telegram_id=12345, invite_code="CF-12345")
        unallocated = Envelope(user_id=12345, name="Нераспределённые", current_amount=10000.0)
        food = Envelope(user_id=12345, name="Продукты", current_amount=1000.0, target_amount=15000.0)
        session.add_all([user, unallocated, food])
        await session.flush()
        
        tx1 = Transaction(user_id=12345, amount=10000.0, envelope_id=unallocated.id, description="Старт")
        tx2 = Transaction(user_id=12345, amount=1000.0, envelope_id=food.id, description="Старт продукты")
        session.add_all([tx1, tx2])
        await session.commit()
        
        food_id = food.id
        unallocated_id = unallocated.id

    # 2. Mock AI response for expense exceeding products balance
    brain_resp = BrainResponse(
        thoughts="Spent 3000 on food, balance is 1000. Deficit is 2000. Will cover from unallocated.",
        intent="transaction",
        transactions=[
            TransactionItem(action="expense", amount=3000.0, target_envelope_name="Продукты")
        ],
        show_dashboard=True,
        coach_reply="Учтено: -3000 в Продукты."
    )

    async def mock_process(*args, **kwargs):
        return brain_resp

    monkeypatch.setattr("app.bot.handlers.transactions.process_user_message", mock_process)

    # 3. Process transaction (spend 3000 on food)
    await handle_transaction(mock_message, "потратил 3000 на продукты", state=mock_state)

    # 4. Check balances
    async with test_session_maker() as session:
        envs_res = await session.execute(select(Envelope).where(Envelope.user_id == 12345))
        envs = list(envs_res.scalars().all())
        unallocated_db = next(e for e in envs if e.name == "Нераспределённые")
        food_db = next(e for e in envs if e.name == "Продукты")

        # Balance check:
        # Food: started 1000 - 3000 expense + 2000 transfer from unallocated = 0.0
        assert food_db.current_amount == 0.0
        # Unallocated: started 10000 - 2000 transfer = 8000.0
        assert unallocated_db.current_amount == 8000.0

        # Check transactions
        txs_res = await session.execute(select(Transaction).where(Transaction.user_id == 12345))
        txs = list(txs_res.scalars().all())
        # Expected:
        # 1. Start unallocated (+10k)
        # 2. Start food (+1k)
        # 3. Expense on food (-3k)
        # 4. Transfer from unallocated (-2k)
        # 5. Transfer to food (+2k)
        assert len(txs) == 5

        tx_expense = next(t for t in txs if t.envelope_id == food_db.id and t.amount == -3000.0)
        assert tx_expense.description == "Трата"

        tx_transfer_from = next(t for t in txs if t.envelope_id == unallocated_db.id and t.amount == -2000.0)
        assert tx_transfer_from.description == f"Перенос покрытия овердрафта: {food_db.name}"

        tx_transfer_to = next(t for t in txs if t.envelope_id == food_db.id and t.amount == 2000.0)
        assert tx_transfer_to.description == "Покрытие овердрафта из Нераспределённых"

        # Verify ledger
        assert await verify_user_ledger(session, 12345) is True


@pytest.mark.asyncio
async def test_ledger_guard_violation_rollback(test_session_maker, mock_message, mock_state, monkeypatch):
    monkeypatch.setattr("app.bot.handlers.transactions.async_session_maker", lambda: test_session_maker())

    # 1. Setup user, envelopes, transactions
    async with test_session_maker() as session:
        user = User(telegram_id=12345, invite_code="CF-12345")
        unallocated = Envelope(user_id=12345, name="Нераспределённые", current_amount=10000.0)
        session.add_all([user, unallocated])
        await session.flush()
        
        tx = Transaction(user_id=12345, amount=10000.0, envelope_id=unallocated.id, description="Старт")
        session.add(tx)
        await session.commit()

    # 2. Mock AI response for normal expense
    brain_resp = BrainResponse(
        thoughts="Spend 1000.",
        intent="transaction",
        transactions=[
            TransactionItem(action="expense", amount=1000.0, target_envelope_name="Нераспределённые")
        ],
        show_dashboard=True,
        coach_reply="Учтено: -1000 в Нераспределённые."
    )

    async def mock_process(*args, **kwargs):
        return brain_resp

    monkeypatch.setattr("app.bot.handlers.transactions.process_user_message", mock_process)

    # 3. Monkeypatch verify_user_ledger to return False, forcing rollback
    monkeypatch.setattr("app.bot.handlers.transactions.verify_user_ledger", AsyncMock(return_value=False))

    # 4. Handle transaction — it should try to commit but rollback since verify_user_ledger returns False
    await handle_transaction(mock_message, "потратил 1000", state=mock_state)

    # 5. Verify database was NOT modified (i.e. rolled back)
    async with test_session_maker() as session:
        envs_res = await session.execute(select(Envelope).where(Envelope.user_id == 12345))
        envs = list(envs_res.scalars().all())
        unallocated_db = next(e for e in envs if e.name == "Нераспределённые")
        # Should remain 10000 because changes were rolled back!
        assert unallocated_db.current_amount == 10000.0

        txs_res = await session.execute(select(Transaction).where(Transaction.user_id == 12345))
        txs = list(txs_res.scalars().all())
        assert len(txs) == 1  # No new transaction saved
        assert txs[0].amount == 10000.0
