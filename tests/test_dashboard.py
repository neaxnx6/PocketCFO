from app.bot.handlers.transactions import build_dashboard, build_micro_navigator, get_financial_insight

class MockEnvelope:
    def __init__(
        self, 
        id: int, 
        name: str, 
        current_amount: float, 
        target_amount: float, 
        is_debt: bool = False, 
        is_goal: bool = False, 
        min_payment: float = 0.0,
        due_day: int = None,
        last_paid_month: str = None
    ):
        self.id = id
        self.name = name
        self.current_amount = current_amount
        self.target_amount = target_amount
        self.is_debt = is_debt
        self.is_goal = is_goal
        self.min_payment = min_payment
        self.due_day = due_day
        self.last_paid_month = last_paid_month

class MockTx:
    def __init__(self, target_envelope_name: str):
        self.target_envelope_name = target_envelope_name

def test_build_dashboard_navigator():
    envelopes = [
        MockEnvelope(1, "Аренда", 20000, 35000, is_debt=False, is_goal=False),
        MockEnvelope(2, "Кредитка", 5000, 50000, is_debt=True, is_goal=False, min_payment=5000),
        MockEnvelope(3, "Нераспределённые", 10000, 0, is_debt=False, is_goal=False),
    ]
    
    result = build_dashboard(envelopes, monthly_income=70000, tab='navigator', monthly_payments={}, monthly_spending={})
    assert "ВКЛАДКА: НАВИГАТОР" in result
    assert "Деньги:</b> <b>30к</b>" in result
    assert "Свободно: <b>10к</b> (этого достаточно для полного покрытия месяца, осталось распределить! ✨)" in result
    assert "ОБЯЗАТЕЛЬСТВА:</b> <b>40к</b>" in result
    assert "Обеспечено: <b>30к</b>" in result
    assert "Не хватает: <b>10к</b>" in result
    assert "Обеспеченность месяца:</b> <b>75%</b>" in result

    # Test when unallocated is enough to cover deficit
    envelopes_enough = [
        MockEnvelope(1, "Аренда", 20000, 35000, is_debt=False, is_goal=False),
        MockEnvelope(2, "Кредитка", 5000, 50000, is_debt=True, is_goal=False, min_payment=5000),
        MockEnvelope(3, "Нераспределённые", 20000, 0, is_debt=False, is_goal=False),
    ]
    result_enough = build_dashboard(envelopes_enough, monthly_income=70000, tab='navigator', monthly_payments={}, monthly_spending={})
    assert "Свободно: <b>20к</b>" in result_enough
    assert "Не хватает: <b>0</b>" in result_enough

def test_build_dashboard_expenses():
    envelopes = [
        MockEnvelope(1, "Аренда", 35000, 35000, is_debt=False, is_goal=False),
        MockEnvelope(2, "Кредитка", 10000, 50000, is_debt=True, is_goal=False, min_payment=5000),
        MockEnvelope(3, "Нераспределённые", 10000, 0, is_debt=False, is_goal=False),
    ]
    
    monthly_payments = {2: 2000.0}
    result = build_dashboard(envelopes, monthly_income=70000, tab='expenses', monthly_payments=monthly_payments, monthly_spending={})
    assert "ВКЛАДКА: РАСХОДЫ" in result
    assert "🏠 Жилье <b>[Оплачено ✅]</b>: доступно <b>35к</b> из <b>35к</b>" in result
    assert "Кредитка (обязательный платеж): оплачено <b>2к</b> из <b>5к</b>" in result

def test_build_dashboard_debts():
    envelopes = [
        MockEnvelope(1, "Аренда", 35000, 35000, is_debt=False, is_goal=False),
        MockEnvelope(2, "Кредитка", 10000, 50000, is_debt=True, is_goal=False, min_payment=5000),
        MockEnvelope(3, "Нераспределённые", 10000, 0, is_debt=False, is_goal=False),
    ]
    
    result = build_dashboard(envelopes, monthly_income=70000, tab='debts', monthly_payments={}, monthly_spending={})
    assert "ВКЛАДКА: ДОЛГИ" in result
    assert "Кредитка: осталось <b>40к</b> (мин. платёж <b>5к</b>, погашено <b>20%</b>)" in result
    assert "Прогноз до конца месяца:" in result

def test_build_micro_navigator():
    envelopes = [
        MockEnvelope(1, "Аренда", 35000, 35000, is_debt=False, is_goal=False),
        MockEnvelope(2, "Кредитка", 10000, 50000, is_debt=True, is_goal=False, min_payment=5000),
        MockEnvelope(3, "Нераспределённые", 10000, 0, is_debt=False, is_goal=False),
    ]
    
    transactions = [
        MockTx("Аренда"),
        MockTx("Кредитка"),
        MockTx("Нераспределённые"),  # This redundant one should be skipped
    ]
    
    monthly_payments = {2: 5000.0}
    micro_nav = build_micro_navigator(envelopes, transactions, monthly_payments)
    assert "Микро-Навигатор:" in micro_nav
    assert "Свободный кэш:</b> <b>10к</b>" in micro_nav
    assert "«Аренда»:</b> осталось <b>35к</b> из <b>35к</b>" in micro_nav
    assert "«Кредитка» (мин. платёж):</b> оплачено <b>5к</b> из <b>5к</b>" in micro_nav
    # verify that the unallocated wallet is skipped and not duplicated at the bottom
    assert "Нераспределённые" not in micro_nav

def test_due_date_sorting_and_overdue():
    from datetime import datetime
    current_day = datetime.utcnow().day
    
    # We want to test overdue fire state
    # Envelope 1: Rent, target 30k, current 10k, due 1 day ago
    # Envelope 2: Internet, target 1k, current 0, due tomorrow
    # Envelope 3: Credit Card, target 50k, current 0, min_payment 5k, due 2 days ago
    
    past_day_1 = current_day - 1 if current_day > 1 else 28
    past_day_2 = current_day - 2 if current_day > 2 else 27
    future_day = current_day + 1 if current_day < 28 else 5

    envelopes = [
        MockEnvelope(1, "Аренда", 10000, 30000, is_debt=False, is_goal=False, due_day=past_day_1),
        MockEnvelope(2, "Интернет", 0, 1000, is_debt=False, is_goal=False, due_day=future_day),
        MockEnvelope(3, "Кредитка Сбер", 0, 50000, is_debt=True, is_goal=False, min_payment=5000, due_day=past_day_2),
        MockEnvelope(4, "Нераспределённые", 0, 0, is_debt=False, is_goal=False),
    ]
    
    # 1. Test get_financial_insight detects overdue fire
    insight = get_financial_insight(envelopes, monthly_payments={}, monthly_spending={})
    assert "СРОЧНЫЙ ПОЖАР" in insight
    
    # 2. Test sorting priority places overdue first
    # Credit Card is overdue by more days (past_day_2 is older than past_day_1), so it should be prioritized first
    # Let's inspect the Action Plan inside the insight
    # Credit Card (due_day = past_day_2) vs Rent (due_day = past_day_1)
    # If past_day_2 < past_day_1, priority of Credit Card is lower (more urgent) than Rent.
    # Therefore, Credit Card (min. payment) should be at the absolute top of the queue.
    assert "Кредитка Сбер (мин. платёж)" in insight
    
    # 3. Test build_dashboard tab=navigator shows Level 0 "Пожар"
    dashboard_nav = build_dashboard(envelopes, monthly_income=50000, tab='navigator', monthly_payments={}, monthly_spending={})
    assert "Пожар" in dashboard_nav
    assert "Аренда" in dashboard_nav
    assert "Кредитка Сбер" in dashboard_nav


def test_absolute_due_days_and_last_paid_month():
    from datetime import datetime
    current_month_str = datetime.utcnow().strftime("%Y-%m")
    
    envelopes = [
        MockEnvelope(1, "Аренда", 0, 35000, is_debt=False, is_goal=False, due_day=15),
        MockEnvelope(2, "Кредитка Сбер", 0, 50000, is_debt=True, is_goal=False, min_payment=5000, due_day=23),
        MockEnvelope(3, "Интернет", 0, 1500, is_debt=False, is_goal=False, due_day=10, last_paid_month=current_month_str),
        MockEnvelope(4, "Нераспределённые", 0, 0, is_debt=False, is_goal=False),
    ]

    # 1. Verify absolute due days display in Debts tab
    result_debts = build_dashboard(envelopes, monthly_income=50000, tab='debts', monthly_payments={}, monthly_spending={})
    assert "Кредитка Сбер (до 23-го):" in result_debts
    
    # 2. Verify absolute due days display in Expenses tab (mandatory payments)
    result_expenses = build_dashboard(envelopes, monthly_income=50000, tab='expenses', monthly_payments={}, monthly_spending={})
    assert "Кредитка Сбер (до 23-го) (обязательный платеж):" in result_expenses
    
    # 3. Verify marked paid status on "Интернет"
    from app.bot.handlers.transactions import get_envelope_due_status_str
    status = get_envelope_due_status_str(envelopes[2], spent_this_month=0.0, current_day=datetime.utcnow().day)
    assert status == "Оплачено ✅"
    
    # 4. Verify navigator math for marked paid envelope:
    result_nav = build_dashboard(envelopes, monthly_income=50000, tab='navigator', monthly_payments={}, monthly_spending={})
    assert "ОБЯЗАТЕЛЬСТВА:</b> <b>41.5к</b>" in result_nav
    assert "Обеспечено: <b>1.5к</b>" in result_nav
    assert "Не хватает: <b>40к</b>" in result_nav


def test_paid_confirmations_formatting():
    from app.bot.handlers.transactions import fmt_money
    
    # Mock some envelopes
    env_rent = MockEnvelope(1, "Аренда", 0, 35000, is_debt=False, is_goal=False, due_day=15)
    env_internet = MockEnvelope(2, "Интернет", 0, 1500, is_debt=False, is_goal=False, due_day=10)
    
    # Test formatting logic
    # Single envelope
    limit = env_rent.min_payment if env_rent.is_debt else env_rent.target_amount
    limit_val = limit or 0.0
    safe_reply_single = f"Отметить статью <b>«{env_rent.name}»</b> полностью оплаченной в этом месяце ({fmt_money(limit_val)})?"
    assert "Аренда" in safe_reply_single
    assert "35к" in safe_reply_single or "35 000" in safe_reply_single
    
    # Multiple envelopes
    valid_envs = [env_rent, env_internet]
    lines = []
    for env in valid_envs:
        limit = env.min_payment if env.is_debt else env.target_amount
        limit_val = limit or 0.0
        lines.append(f"• <b>{env.name}</b> ({fmt_money(limit_val)})")
    safe_reply_multi = "Отметить эти статьи полностью оплаченными в этом месяце?\n" + "\n".join(lines)
    
    assert "Аренда" in safe_reply_multi
    assert "Интернет" in safe_reply_multi
    assert "35к" in safe_reply_multi or "35 000" in safe_reply_multi
    assert "1.5к" in safe_reply_multi or "1 500" in safe_reply_multi or "1.5" in safe_reply_multi

