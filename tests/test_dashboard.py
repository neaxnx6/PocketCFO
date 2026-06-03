from app.bot.handlers.transactions import build_dashboard, build_micro_navigator

class MockEnvelope:
    def __init__(self, id: int, name: str, current_amount: float, target_amount: float, is_debt: bool = False, is_goal: bool = False, min_payment: float = 0.0):
        self.id = id
        self.name = name
        self.current_amount = current_amount
        self.target_amount = target_amount
        self.is_debt = is_debt
        self.is_goal = is_goal
        self.min_payment = min_payment

class MockTx:
    def __init__(self, target_envelope_name: str):
        self.target_envelope_name = target_envelope_name

def test_build_dashboard_navigator():
    envelopes = [
        MockEnvelope(1, "Аренда", 35000, 35000, is_debt=False, is_goal=False),
        MockEnvelope(2, "Кредитка", 10000, 50000, is_debt=True, is_goal=False, min_payment=5000),
        MockEnvelope(3, "Нераспределённые", 10000, 0, is_debt=False, is_goal=False),
    ]
    
    result = build_dashboard(envelopes, monthly_income=70000, tab='navigator', monthly_payments={})
    assert "ВКЛАДКА: НАВИГАТОР" in result
    assert "ОБЯЗАТЕЛЬСТВА В ЭТОМ МЕСЯЦЕ:</b> 40к" in result
    assert "Обеспечено:</b> 45к" in result
    assert "Не хватает (дефицит):</b> 0" in result

def test_build_dashboard_expenses():
    envelopes = [
        MockEnvelope(1, "Аренда", 35000, 35000, is_debt=False, is_goal=False),
        MockEnvelope(2, "Кредитка", 10000, 50000, is_debt=True, is_goal=False, min_payment=5000),
        MockEnvelope(3, "Нераспределённые", 10000, 0, is_debt=False, is_goal=False),
    ]
    
    monthly_payments = {2: 2000.0}
    result = build_dashboard(envelopes, monthly_income=70000, tab='expenses', monthly_payments=monthly_payments)
    assert "ВКЛАДКА: РАСХОДЫ И ЛИМИТЫ" in result
    assert "Аренда: доступно 35к" in result
    assert "Кредитка (мин. платёж): оплачено 2к из 5к" in result

def test_build_dashboard_debts():
    envelopes = [
        MockEnvelope(1, "Аренда", 35000, 35000, is_debt=False, is_goal=False),
        MockEnvelope(2, "Кредитка", 10000, 50000, is_debt=True, is_goal=False, min_payment=5000),
        MockEnvelope(3, "Нераспределённые", 10000, 0, is_debt=False, is_goal=False),
    ]
    
    result = build_dashboard(envelopes, monthly_income=70000, tab='debts')
    assert "ВКЛАДКА: ДОЛГОВЫЕ ОБЯЗАТЕЛЬСТВА" in result
    assert "Кредитка: осталось 40к" in result
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
    ]
    
    monthly_payments = {2: 5000.0}
    micro_nav = build_micro_navigator(envelopes, transactions, monthly_payments)
    assert "Микро-Навигатор:" in micro_nav
    assert "Свободный кэш:</b> 10к" in micro_nav
    assert "«Аренда»:</b> осталось 35к" in micro_nav
    assert "«Кредитка» (мин. платёж):</b> оплачено 5к из 5к" in micro_nav
