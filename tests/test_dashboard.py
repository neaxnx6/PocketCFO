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
        MockEnvelope(1, "Аренда", 20000, 35000, is_debt=False, is_goal=False),
        MockEnvelope(2, "Кредитка", 5000, 50000, is_debt=True, is_goal=False, min_payment=5000),
        MockEnvelope(3, "Нераспределённые", 10000, 0, is_debt=False, is_goal=False),
    ]
    
    result = build_dashboard(envelopes, monthly_income=70000, tab='navigator', monthly_payments={})
    assert "ВКЛАДКА: НАВИГАТОР" in result
    assert "Деньги:</b> <b>30к</b>" in result
    assert "Свободно: <b>10к</b> (хватит, чтобы покрыть еще <b>10к</b> обязательств 💡)" in result
    assert "ОБЯЗАТЕЛЬСТВА:</b> <b>40к</b>" in result
    assert "Обеспечено: <b>25к</b>" in result
    assert "Не хватает: <b>15к</b>" in result
    assert "Обеспеченность месяца:</b> <b>62%</b>" in result

    # Test when unallocated is enough to cover deficit
    envelopes_enough = [
        MockEnvelope(1, "Аренда", 20000, 35000, is_debt=False, is_goal=False),
        MockEnvelope(2, "Кредитка", 5000, 50000, is_debt=True, is_goal=False, min_payment=5000),
        MockEnvelope(3, "Нераспределённые", 20000, 0, is_debt=False, is_goal=False),
    ]
    result_enough = build_dashboard(envelopes_enough, monthly_income=70000, tab='navigator', monthly_payments={})
    assert "Свободно: <b>20к</b> (этого достаточно для полного покрытия месяца, осталось распределить! ✨)" in result_enough

def test_build_dashboard_expenses():
    envelopes = [
        MockEnvelope(1, "Аренда", 35000, 35000, is_debt=False, is_goal=False),
        MockEnvelope(2, "Кредитка", 10000, 50000, is_debt=True, is_goal=False, min_payment=5000),
        MockEnvelope(3, "Нераспределённые", 10000, 0, is_debt=False, is_goal=False),
    ]
    
    monthly_payments = {2: 2000.0}
    result = build_dashboard(envelopes, monthly_income=70000, tab='expenses', monthly_payments=monthly_payments)
    assert "ВКЛАДКА: РАСХОДЫ" in result
    assert "🏠 Жилье: доступно <b>35к</b> из <b>35к</b>" in result
    assert "Кредитка (обязательный платеж): оплачено <b>2к</b> из <b>5к</b>" in result

def test_build_dashboard_debts():
    envelopes = [
        MockEnvelope(1, "Аренда", 35000, 35000, is_debt=False, is_goal=False),
        MockEnvelope(2, "Кредитка", 10000, 50000, is_debt=True, is_goal=False, min_payment=5000),
        MockEnvelope(3, "Нераспределённые", 10000, 0, is_debt=False, is_goal=False),
    ]
    
    result = build_dashboard(envelopes, monthly_income=70000, tab='debts')
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
