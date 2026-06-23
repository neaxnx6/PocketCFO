with open("app/bot/handlers/transactions.py", "r", encoding="utf-8") as f:
    content = f.read()

# 1. Remove buttons inside show_group_details
old_buttons_block = """            if remaining_limit == 0:
                lines.append(f"• {e.name}{due_str}: Оплачено в этом месяце ✅{limit_str}")
            else:
                lines.append(f"• {e.name}{due_str}: Нужно закрыть <b>{fmt_money(remaining_limit)}</b> (на балансе: <b>{fmt_money(e.current_amount)}</b>)")
                # Add button to mark as paid outside
                buttons.append([InlineKeyboardButton(
                    text=f"✅ {e.name} уже оплачено вне бота", 
                    callback_data=f"mark_paid:{grp_name}:{e.id}"
                )])"""
new_buttons_block = """            if remaining_limit <= 0:
                lines.append(f"• {e.name}{due_str}: Оплачено в этом месяце ✅{limit_str}")
            else:
                lines.append(f"• {e.name}{due_str}: Нужно закрыть <b>{fmt_money(remaining_limit)}</b> (на балансе: <b>{fmt_money(e.current_amount)}</b>)")"""
content = content.replace(old_buttons_block, new_buttons_block)

import re
# 2. Remove mark_paid_callback completely
# From @router.callback_query(F.data.startswith("mark_paid:"))
# To the end of back_to_expenses_callback
# Wait, I shouldn't remove back_to_expenses_callback.
# Let's match from @router.callback_query(F.data.startswith("mark_paid:")) to the start of back_to_expenses_callback

pattern = re.compile(r'@router\.callback_query\(F\.data\.startswith\("mark_paid:"\)\).*?(?=@router\.callback_query\(F\.data == "back_to_expenses"\))', re.DOTALL)
content = pattern.sub('', content)

with open("app/bot/handlers/transactions.py", "w", encoding="utf-8") as f:
    f.write(content)

print("Buttons patched")
