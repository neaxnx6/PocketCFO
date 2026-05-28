import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.database.models import Base, User, Envelope, Transaction
from app.bot.handlers.family import generate_invite_code

# Setup test db
TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

@pytest_asyncio.fixture
async def test_session_maker():
    engine = create_async_engine(TEST_DB_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()

@pytest.mark.asyncio
async def test_invite_code_generation():
    code = generate_invite_code()
    assert code.startswith("CF-")
    assert len(code) == 9

@pytest.mark.asyncio
async def test_family_linking(test_session_maker):
    async with test_session_maker() as session:
        # Create two users
        host = User(telegram_id=111, invite_code="CF-111111")
        member = User(telegram_id=222, invite_code="CF-222222")
        session.add_all([host, member])
        await session.commit()
        
        # Link member to host
        member.family_host_id = host.telegram_id
        member.invite_code = None
        await session.commit()
        
        # Query and assert
        assert member.family_host_id == host.telegram_id
        assert member.invite_code is None
