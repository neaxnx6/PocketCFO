from app.bot.handlers.transactions import calculate_forecasts, fmt_months_ru

class MockEnvelope:
    def __init__(self, name: str, current_amount: float, target_amount: float, is_debt: bool = False, is_goal: bool = False):
        self.name = name
        self.current_amount = current_amount
        self.target_amount = target_amount
        self.is_debt = is_debt
        self.is_goal = is_goal

def test_calculate_forecasts_ok():
    # Envelopes setup:
    # Expense: Аренда 30к
    # Debt: Кредитка, цель 50к, погашено 10к (осталось 40к)
    # Goal: Ноутбук, цель 80к, накоплено 0к
    envelopes = [
        MockEnvelope("Аренда", 30000, 30000, is_debt=False, is_goal=False),
        MockEnvelope("Кредитка", 10000, 50000, is_debt=True, is_goal=False),
        MockEnvelope("Ноутбук", 0, 80000, is_debt=False, is_goal=True),
        MockEnvelope("Нераспределённые", 5000, 0, is_debt=False, is_goal=False),
    ]
    # monthly_income = 70000. monthly_expenses = 30000. free_cash = 40000
    res = calculate_forecasts(envelopes, monthly_income=70000)
    assert res["status"] == "ok"
    assert res["free_cash"] == 40000
    # Кредитка (остаток 40к) должна закрыться в месяц 1
    assert res["completed"]["Кредитка"] == 1
    # Ноутбук (остаток 80к) должен закрыться через (40к в месяц 2 + 40к в месяц 3) = 3 месяца
    assert res["completed"]["Ноутбук"] == 3

def test_calculate_forecasts_negative():
    envelopes = [
        MockEnvelope("Аренда", 30000, 30000, is_debt=False, is_goal=False),
    ]
    # free_cash = 25000 - 30000 = -5000
    res = calculate_forecasts(envelopes, monthly_income=25000)
    assert res["status"] == "negative_or_zero"

def test_fmt_months_ru():
    assert fmt_months_ru(1) == "в следующем месяце"
    assert fmt_months_ru(2) == "через 2 месяца"
    assert fmt_months_ru(5) == "через 5 месяцев"
    assert fmt_months_ru(21) == "через 21 месяц"
    assert fmt_months_ru(22) == "через 22 месяца"
    assert fmt_months_ru(25) == "через 25 месяцев"
