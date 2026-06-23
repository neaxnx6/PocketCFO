import re

with open("app/bot/handlers/transactions.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Update get_sorting_priority signature
content = content.replace(
    "def get_sorting_priority(e, current_day: int, monthly_payments: dict, monthly_spending: dict) -> int:",
    "def get_sorting_priority(e, current_day: int, monthly_payments: dict, monthly_spending: dict, monthly_adjustments: dict = None) -> int:\n    monthly_adjustments = monthly_adjustments or {}"
)

# Remove last_paid_month from get_sorting_priority
content = re.sub(
    r'    current_month_str = datetime\.utcnow\(\)\.strftime\("%Y-%m"\)\n    if getattr\(e, \'last_paid_month\', None\) == current_month_str:\n        return 999\n',
    '',
    content
)

# Update get_sorting_priority logic
old_logic = """    if getattr(e, 'is_debt', False):
        paid = monthly_payments.get(e.id, 0.0)
        min_pay = getattr(e, 'min_payment', 0.0) or 0.0
        is_paid = paid >= min_pay
    else:
        spent = monthly_spending.get(e.id, 0.0)
        target = getattr(e, 'target_amount', 0.0) or 0.0
        is_paid = (getattr(e, 'current_amount', 0.0) + spent) >= target"""

new_logic = """    adj_val = monthly_adjustments.get(e.id, 0.0)
    if getattr(e, 'is_debt', False):
        paid = monthly_payments.get(e.id, 0.0) + adj_val
        min_pay = getattr(e, 'min_payment', 0.0) or 0.0
        is_paid = paid >= min_pay
    else:
        spent = monthly_spending.get(e.id, 0.0) + adj_val
        target = getattr(e, 'target_amount', 0.0) or 0.0
        is_paid = (getattr(e, 'current_amount', 0.0) + spent) >= target"""
content = content.replace(old_logic, new_logic)

# Update calls to get_sorting_priority
content = content.replace(
    "key=lambda x: get_sorting_priority(x, current_day, monthly_payments, monthly_spending)",
    "key=lambda x: get_sorting_priority(x, current_day, monthly_payments, monthly_spending, monthly_adjustments)"
)
content = content.replace(
    "key=lambda x: get_sorting_priority(x[0], current_day, monthly_payments, monthly_spending)",
    "key=lambda x: get_sorting_priority(x[0], current_day, monthly_payments, monthly_spending, monthly_adjustments)"
)

# 2. Update get_envelope_due_status_str
content = content.replace(
    "def get_envelope_due_status_str(e, spent_this_month: float, current_day: int) -> str:",
    "def get_envelope_due_status_str(e, spent_this_month: float, current_day: int, adj_val: float = 0.0) -> str:\n    spent_this_month += adj_val"
)
content = re.sub(
    r'    current_month_str = datetime\.utcnow\(\)\.strftime\("%Y-%m"\)\n    if getattr\(e, \'last_paid_month\', None\) == current_month_str:\n        return "Оплачено ✅"\n',
    '',
    content
)

# Update calls to get_envelope_due_status_str
# Line 801
content = re.sub(r'status = get_envelope_due_status_str\(env, paid_min, current_day\)', 'status = get_envelope_due_status_str(env, paid_min, current_day, monthly_adjustments.get(env.id, 0.0))', content)
# Line 967
content = re.sub(r'status = get_envelope_due_status_str\(d, paid_this_month, current_day\)', 'status = get_envelope_due_status_str(d, paid_this_month, current_day, monthly_adjustments.get(d.id, 0.0))', content)
# Line 985
content = re.sub(r'status = get_envelope_due_status_str\(e, paid_this_month, current_day\)', 'status = get_envelope_due_status_str(e, paid_this_month, current_day, monthly_adjustments.get(e.id, 0.0))', content)

# 3. Update build_dashboard "is_settled" check (Line ~937)
old_is_settled = """                        is_settled = (
                            getattr(e, 'last_paid_month', None) == current_month_str 
                            or e.current_amount >= rem_limit
                        )"""
new_is_settled = """                        is_settled = (rem_limit <= 0)"""
content = content.replace(old_is_settled, new_is_settled)

# 4. Remove "Очередь финансирования" block from build_dashboard
old_deficit_block = """        if deficit_lines:
            deficit_text = "<b>Очередь финансирования:</b>\\n" + "\\n".join(deficit_lines)
            parts.append(deficit_text)
            
"""
content = content.replace(old_deficit_block, "")

# 5. Remove "Оплачено в этом месяце" logic from `get_sorting_priority` usage if any other left over
# It should be fine.

with open("app/bot/handlers/transactions.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Patch applied")
