import logging
from aiogram import Router, F, Bot
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.future import select
from sqlalchemy import func

from app.database.session import async_session_maker
from app.database.models import User, Transaction, Envelope, ChatMessage
from app.services.ai_brain import process_user_message, IncomeAllocation
from app.services.voice_service import transcribe_voice

router = Router()
logger = logging.getLogger(__name__)


DASHBOARD_TRIGGERS = [
    "сколько осталось", "сколько денег", "баланс", "остаток",
    "какой остаток", "что по фондам", "сколько в фонд",
    "сколько у меня", "покажи бюджет", "покажи фонд",
    "мои деньги", "мои фонды", "сводка", "дашборд",
    "мой бюджет", "📊 мой бюджет",
]


def fmt_money(val: float) -> str:
    """Format money: 216750 -> '216.8к', 5000 -> '5к', 1500.5 -> '1.5к'"""
    if val >= 1000:
        return f"{val/1000:.1f}к".replace('.0к', 'к')
    elif val == int(val):
        return f"{int(val)}"
    else:
        return f"{val:.1f}".rstrip('0').rstrip('.')


def _is_dashboard_request(text: str) -> bool:
    text_lower = text.lower().strip()
    for trigger in DASHBOARD_TRIGGERS:
        if trigger in text_lower:
            return True
    return False


class IncomeStates(StatesGroup):
    confirming = State()


def _normalize_name(name: str) -> str:
    """Normalize envelope name for matching: remove prefixes, lowercase, strip."""
    name = name.lower().strip()
    # Remove common prefixes that shouldn't affect matching
    for prefix in ["кредитка ", "долг ", "карта ", "долг маме ", "долг сестре "]:
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.strip()


def _find_envelope(envelopes: list, name: str):
    name_normalized = _normalize_name(name)
    # First try exact match with normalization
    for env in envelopes:
        if _normalize_name(env.name) == name_normalized:
            return env
    # Then try substring match
    for env in envelopes:
        env_norm = _normalize_name(env.name)
        if name_normalized in env_norm or env_norm in name_normalized:
            return env
    return None


def _find_unallocated(envelopes: list):
    for env in envelopes:
        if env.name.lower().strip() in ("нераспределённые", "кошелек", "кошелёк"):
            return env
    return None


def build_envelopes_context(envelopes: list[Envelope]) -> str:
    expense_lines = []
    goal_lines = []
    debt_lines = []
    for e in envelopes:
        if getattr(e, 'is_debt', False):
            remaining = (e.target_amount or 0) - e.current_amount
            debt_lines.append(
                f"- [ДОЛГ] '{e.name}': осталось вернуть {remaining:.0f} руб "
                f"(оплачено {e.current_amount:.0f} из {e.target_amount or 0:.0f})"
            )
        elif getattr(e, 'is_goal', False):
            goal_lines.append(
                f"- [ЦЕЛЬ] '{e.name}': накоплено {e.current_amount:.0f} из {e.target_amount or 0:.0f} руб"
            )
        else:
            limit_str = f", лимит {e.target_amount:.0f}" if e.target_amount else ""
            pct = ""
            if e.target_amount and e.target_amount > 0:
                ratio = e.current_amount / e.target_amount * 100
                pct = f" ({ratio:.0f}%)"
            expense_lines.append(
                f"- [РАСХОД] '{e.name}': {e.current_amount:.0f} руб{limit_str}{pct}"
            )

    parts = []
    if expense_lines:
        parts.append("Статьи расходов:\n" + "\n".join(expense_lines))
    if goal_lines:
        parts.append("Цели/накопления:\n" + "\n".join(goal_lines))
    if debt_lines:
        total_debt = sum(
            (e.target_amount or 0) - e.current_amount
            for e in envelopes if getattr(e, 'is_debt', False)
        )
        parts.append(f"Долги (итого {total_debt:.0f} руб):\n" + "\n".join(debt_lines))
    return "\n".join(parts) if parts else "Бюджет пока пуст"


def build_financial_health(envelopes: list[Envelope], monthly_income: float = 0.0) -> str:
    debt_envs = [e for e in envelopes if getattr(e, 'is_debt', False)]
    expense_envs = [e for e in envelopes if not getattr(e, 'is_debt', False) and not getattr(e, 'is_goal', False)]
    total_debt = sum((e.target_amount or 0) - e.current_amount for e in debt_envs)
    monthly_expenses = (
        sum(e.target_amount or 0 for e in expense_envs if (e.target_amount or 0) > 0)
        + sum(d.min_payment or 0 for d in debt_envs if (d.target_amount or 0) - d.current_amount > 0)
    )

    lines = []
    free_cash = None
    if monthly_income and monthly_income > 0:
        lines.append(f"Месячный доход: {monthly_income:.0f} руб")
    if monthly_expenses > 0:
        lines.append(f"Месячные расходы: {monthly_expenses:.0f} руб")
    if monthly_income and monthly_income > 0 and monthly_expenses > 0:
        free_cash = monthly_income - monthly_expenses
        lines.append(f"Свободный кэш: {free_cash:.0f} руб/мес")
    if total_debt > 0:
        lines.append(f"Общий долг: {total_debt:.0f} руб")
        denominator = free_cash if (free_cash and free_cash > 0) else (monthly_income if monthly_income > 0 else None)
        if denominator and denominator > 0:
            months = total_debt / denominator
            label = "свободного кэша" if (free_cash and free_cash > 0) else "дохода"
            lines.append(f"Месяцев {label} для погашения: {months:.1f}")
            if months > 6:
                lines.append("УРОВЕНЬ: 🚨 Критично (долги > 6 мес)")
            elif months > 3:
                lines.append("УРОВЕНЬ: ⚠️ Напряжённо (долги 3-6 мес)")
            else:
                lines.append("УРОВЕНЬ: ✅ Управляемо (долги < 3 мес)")
        else:
            lines.append("УРОВЕНЬ: неизвестен")
    else:
        lines.append("УРОВЕНЬ: 🟢 Рост (долгов нет)")
    return "\n".join(lines)


def get_health_status(envelopes: list) -> str:
    debt_envs = [e for e in envelopes if getattr(e, 'is_debt', False)]
    expense_envs = [e for e in envelopes if not getattr(e, 'is_debt', False) and not getattr(e, 'is_goal', False)]
    total_debt = sum((e.target_amount or 0) - e.current_amount for e in debt_envs)
    monthly_expenses = (
        sum(e.target_amount or 0 for e in expense_envs if (e.target_amount or 0) > 0)
        + sum(d.min_payment or 0 for d in debt_envs if (d.target_amount or 0) - d.current_amount > 0)
    )
    
    free_cash_env = _find_unallocated(envelopes)
    free_cash = free_cash_env.current_amount if free_cash_env else 0
    denominator = free_cash if free_cash > 0 else monthly_expenses * 0.2
    
    if total_debt > 0:
        months = total_debt / denominator if denominator > 0 else 99
        if months > 6:
            return "🟡 <b>Состояние:</b> Напряженное (в фокусе — гашение долгов)"
        elif months > 3:
            return "🟡 <b>Состояние:</b> Стабильное (долги под контролем)"
        else:
            return "🟢 <b>Состояние:</b> Хорошее (финишная прямая по долгам)"
    return "🟢 <b>Состояние:</b> Отличное (долгов нет, фокус на капитал)"


def calculate_forecasts(envelopes: list, monthly_income: float) -> dict:
    # 1. Расчет месячных расходов (не долги, не цели, не буфер, не нераспределенные)
    expense_envs = [
        e for e in envelopes 
        if not getattr(e, 'is_debt', False) 
        and not getattr(e, 'is_goal', False) 
        and "буфер" not in e.name.lower()
        and e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")
    ]
    debt_envs = [e for e in envelopes if getattr(e, 'is_debt', False)]
    monthly_expenses = (
        sum(e.target_amount or 0 for e in expense_envs if (e.target_amount or 0) > 0)
        + sum(d.min_payment or 0 for d in debt_envs if (d.target_amount or 0) - d.current_amount > 0)
    )
    free_cash = monthly_income - monthly_expenses
    
    if free_cash <= 0:
        return {"status": "negative_or_zero", "free_cash": free_cash}

    # 2. Активные долги (сортировка: сначала меньшие остатки - Snowball)
    active_debts = [d for d in debt_envs if (d.target_amount or 0) - d.current_amount > 0]
    active_debts.sort(key=lambda x: (x.target_amount or 0) - x.current_amount)

    # 3. Активные цели (сортировка по приоритету / остатку)
    goal_envs = [e for e in envelopes if getattr(e, 'is_goal', False) and "буфер" not in e.name.lower()]
    active_goals = [g for g in goal_envs if (g.target_amount or 0) - g.current_amount > 0]

    # Моделирование
    sim_items = []
    for d in active_debts:
        sim_items.append({
            "name": d.name,
            "remaining": (d.target_amount or 0) - d.current_amount,
            "is_debt": True
        })
    for g in active_goals:
        sim_items.append({
            "name": g.name,
            "remaining": (g.target_amount or 0) - g.current_amount,
            "is_debt": False
        })

    completed = {}
    current_month = 0
    max_months = 120
    
    while any(item["remaining"] > 0 for item in sim_items) and current_month < max_months:
        current_month += 1
        available = free_cash
        for item in sim_items:
            if item["remaining"] > 0:
                if available >= item["remaining"]:
                    available -= item["remaining"]
                    item["remaining"] = 0.0
                    completed[item["name"]] = current_month
                else:
                    item["remaining"] -= available
                    available = 0.0
                    break

    return {
        "status": "ok",
        "free_cash": free_cash,
        "completed": completed
    }


def fmt_months_ru(n: int) -> str:
    if n == 1:
        return "в следующем месяце"
    if n % 100 in (11, 12, 13, 14):
        return f"через {n} месяцев"
    elif n % 10 == 1:
        return f"через {n} месяц"
    elif n % 10 in (2, 3, 4):
        return f"через {n} месяца"
    else:
        return f"через {n} месяцев"


def get_financial_insight(envelopes: list, monthly_income: float = 0.0) -> str:
    debt_envs = [e for e in envelopes if getattr(e, 'is_debt', False)]
    buffer_env = next((e for e in envelopes if "буфер" in e.name.lower()), None)
    
    insight = ""
    # 1. Mandatory Credit Card Payments check
    active_debts_with_min = [d for d in debt_envs if (d.min_payment or 0) > 0 and (d.target_amount or 0) - d.current_amount > 0]
    if active_debts_with_min:
        total_min_pay = sum(d.min_payment or 0 for d in active_debts_with_min)
        lines = [f"{d.name} ({fmt_money(d.min_payment)})" for d in active_debts_with_min]
        insight = f"🔥 <b>Следующий шаг:</b> Обеспечить минимальные платежи по кредиткам на сумму <b>{fmt_money(total_min_pay)}</b>:\n" + "\n".join(f"• {l}" for l in lines)
        
    # 2. Zero Buffer Alert
    elif not buffer_env or buffer_env.current_amount <= 0:
        insight = "⚠️ <b>Следующий шаг:</b> С ближайшего дохода нужно заложить хотя бы минимальный буфер. Жить без подушки небезопасно."
    
    # 3. Easy Debt Win
    elif debt_envs and [d for d in debt_envs if (d.target_amount or 0) - d.current_amount > 0]:
        active_debts = [d for d in debt_envs if (d.target_amount or 0) - d.current_amount > 0]
        active_debts.sort(key=lambda x: (x.target_amount or 0) - x.current_amount)
        smallest_debt = active_debts[0]
        remaining = (smallest_debt.target_amount or 0) - smallest_debt.current_amount
        if remaining < 10000:
            insight = f"🔥 <b>Следующий шаг:</b> Добить остаток по «{smallest_debt.name}» ({fmt_money(remaining)}). Один рывок — и минус один долг!"
        else:
            insight = "💡 <b>Фокус:</b> Продолжаем методично гасить кредиты. Каждая тысяча сверх минимума экономит тебе время и проценты."
            
    # 4. Growth Phase
    else:
        goal_envs = [e for e in envelopes if getattr(e, 'is_goal', False) and "буфер" not in e.name.lower()]
        if goal_envs:
            insight = "🚀 <b>Фокус:</b> Бюджет сбалансирован. Все свободные деньги теперь работают на твои цели!"
        else:
            insight = "💡 Бюджет в норме. Распределяй новые доходы по правилу: сначала плати себе (буфер), потом трать."
            
    # Добавляем блок прогнозов
    if monthly_income > 0:
        forecast = calculate_forecasts(envelopes, monthly_income)
        if forecast["status"] == "ok" and forecast["completed"]:
            lines = []
            for name, months in forecast["completed"].items():
                lines.append(f"• {name}: {fmt_months_ru(months)}")
            forecast_text = "\n📅 <b>Если придерживаться плана:</b>\n" + "\n".join(lines)
            insight += "\n" + forecast_text
        elif forecast["status"] == "negative_or_zero":
            insight += "\n⚠️ <i>Расходы превышают доходы или равны им. Сбалансируй бюджет, чтобы начать гасить долги.</i>"
    else:
        insight += "\n💡 <i>Задай месячный доход (например: «мой доход 100к»), чтобы увидеть прогноз закрытия долгов и целей.</i>"
        
    return insight


def build_dashboard(envelopes: list, monthly_income: float = 0.0) -> str:
    expense_lines = []
    goal_lines = []
    credit_lines = []
    personal_lines = []
    unallocated_amount = 0.0
    buffer_env = None

    # Filter out categories
    expense_envs = [
        e for e in envelopes 
        if not getattr(e, 'is_debt', False) 
        and not getattr(e, 'is_goal', False) 
        and "буфер" not in e.name.lower()
        and e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")
    ]
    debt_envs = [e for e in envelopes if getattr(e, 'is_debt', False)]

    for e in envelopes:
        if e.name.lower().strip() in ("нераспределённые", "кошелек", "кошелёк"):
            unallocated_amount = e.current_amount
            continue
            
        if "буфер" in e.name.lower():
            buffer_env = e
            continue
            
        if getattr(e, 'is_debt', False):
            rem = (e.target_amount or 0) - e.current_amount
            if rem > 0:
                pct = int((e.current_amount / e.target_amount * 100)) if e.target_amount else 0
                if (e.min_payment or 0) > 0:
                    min_pay_str = f", мин. платёж {fmt_money(e.min_payment)}"
                    credit_lines.append(f"• {e.name}: осталось {fmt_money(rem)} (всего {fmt_money(e.target_amount or 0)}{min_pay_str}, погашено {pct}%)")
                else:
                    personal_lines.append(f"• {e.name}: осталось {fmt_money(rem)} (всего {fmt_money(e.target_amount or 0)}, погашено {pct}%)")
        elif getattr(e, 'is_goal', False):
            target_str = f" (цель {fmt_money(e.target_amount or 0)})" if (e.target_amount or 0) > 0 else ""
            goal_lines.append(f"• {e.name}: накоплено {fmt_money(e.current_amount)}{target_str}")
        else:
            # Check funded status emoji
            status_emoji = "✅" if e.current_amount >= (e.target_amount or 0) else "❌"
            expense_lines.append(f"{status_emoji} {e.name}: доступно {fmt_money(e.current_amount)} (лимит {fmt_money(e.target_amount or 0)})")

    parts = ["📊 <b>Финансовый навигатор</b>\n"]
    
    # Emotional UX: Health Status & Insights
    if len(envelopes) > 1:
        parts.append(get_health_status(envelopes))
        
        # Calculate obligations and deficit
        total_obligations = (
            sum(e.target_amount or 0 for e in expense_envs)
            + sum(d.min_payment or 0 for d in debt_envs if (d.target_amount or 0) - d.current_amount > 0)
        )
        total_funded = sum(e.current_amount for e in expense_envs)
        deficit = max(0.0, total_obligations - total_funded)
        
        parts.append(
            f"🔴 <b>Всего обязательств в этом месяце:</b> {fmt_money(total_obligations)}\n"
            f"🟢 <b>Обеспечено деньгами:</b> {fmt_money(total_funded)}\n"
            f"🟡 <b>Не хватает:</b> {fmt_money(deficit)}"
        )
        parts.append(get_financial_insight(envelopes, monthly_income=monthly_income) + "\n")

    if expense_lines:
        parts.append("🛍 <b>На расходы:</b>\n" + "\n".join(expense_lines))
    
    if buffer_env:
        parts.append(f"\n🛡 <b>Буфер:</b> {fmt_money(buffer_env.current_amount)}")
    else:
        parts.append("\n🛡 <b>Буфер:</b> 0")
        
    if goal_lines:
        parts.append("\n🎯 <b>Цели:</b>\n" + "\n".join(goal_lines))
        
    if credit_lines:
        parts.append("\n💳 <b>Кредиты и карты:</b>\n" + "\n".join(credit_lines))
        
    if personal_lines:
        parts.append("\n🤝 <b>Долги близким:</b>\n" + "\n".join(personal_lines))
        
    if unallocated_amount > 0:
        parts.append(f"\n💵 <b>Свободный кэш:</b> {fmt_money(unallocated_amount)}")
        
    if len(parts) == 1 and unallocated_amount == 0:
        return "Твой бюджет пока пуст. Расскажи, сколько зарабатываешь, какие есть обязательные расходы и долги, и я составлю финансовый план! 🚀"
        
    return "\n".join(parts)


def validate_plan_math(response) -> str:
    if response.intent != "profile_update" or not response.plan_items or response.free_cash is None:
        return response.coach_reply
    plan_total = sum(item.amount for item in response.plan_items)
    if plan_total <= response.free_cash:
        return response.coach_reply
    return response.coach_reply + (
        "\n\n💡 <i>Мой план чуть превышает доступные средства. "
        "Давай выберем, что сейчас в приоритете.</i>"
    )


async def handle_transaction(message: Message, text: str, state: FSMContext = None):
    try:
        async with async_session_maker() as session:
            result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
            user = result.scalar_one_or_none()
            if not user:
                await message.answer("Сначала нажми /start")
                return

            budget_owner = user
            if user.family_host_id:
                host_result = await session.execute(select(User).where(User.telegram_id == user.family_host_id))
                host = host_result.scalar_one_or_none()
                if host:
                    budget_owner = host
                else:
                    user.family_host_id = None
                    await session.flush()

            env_result = await session.execute(select(Envelope).where(Envelope.user_id == budget_owner.telegram_id))
            envelopes = list(env_result.scalars().all())
            if not envelopes:
                env = Envelope(user_id=budget_owner.telegram_id, name="Нераспределённые", current_amount=0)
                session.add(env)
                await session.commit()
                envelopes = [env]

            if _is_dashboard_request(text):
                session.add(ChatMessage(user_id=user.telegram_id, role="user", content=text))
                dashboard = build_dashboard(envelopes, monthly_income=budget_owner.monthly_income or 0)
                session.add(ChatMessage(user_id=user.telegram_id, role="assistant", content=dashboard))
                await session.commit()
                reply_markup = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="👥 Семейный бюджет", callback_data="family_menu")]
                ])
                await message.answer(dashboard, parse_mode="HTML", reply_markup=reply_markup)
                return

            loading_msgs = []
            animation_task = None
            if True:  # Always run preloader
                import asyncio
                
                async def animate_messages(chat_id, phrases):
                    """Animate a sequence of messages in background."""
                    current_msg = await message.answer(f"✨ {phrases[0]}...", parse_mode="HTML")
                    loading_msgs.append(current_msg)
                    try:
                        dots = ["...", "..", ".", ""]
                        for i, phrase in enumerate(phrases):
                            duration = 3 if i < len(phrases) - 1 else 999
                            start_time = asyncio.get_event_loop().time()
                            while asyncio.get_event_loop().time() - start_time < duration:
                                for d in dots:
                                    try:
                                        await current_msg.edit_text(f"✨ {phrase}{d}", parse_mode="HTML")
                                    except Exception:
                                        pass
                                    await asyncio.sleep(0.75)
                            
                            if i < len(phrases) - 1:
                                next_msg = await message.answer(f"✨ {phrases[i+1]}...", parse_mode="HTML")
                                loading_msgs.append(next_msg)
                                try:
                                    await current_msg.delete()
                                except Exception:
                                    pass
                                current_msg = next_msg
                    except asyncio.CancelledError:
                        pass # Task cancelled, all good
                    finally:
                        for msg in loading_msgs:
                            try:
                                await msg.delete()
                            except Exception:
                                pass
                
                phrases = ["Принял, анализирую", "Подбиваю цифры", "Формирую план", "Почти готово"]
                animation_task = asyncio.create_task(animate_messages(message.chat.id, phrases))

            env_context = build_envelopes_context(envelopes)
            financial_health = build_financial_health(envelopes, monthly_income=budget_owner.monthly_income or 0)

            chat_result = await session.execute(
                select(ChatMessage)
                .where(ChatMessage.user_id == user.telegram_id)
                .order_by(ChatMessage.datetime_created.desc())
                .limit(12)
            )
            chat_history_db = list(reversed(chat_result.scalars().all()))
            chat_history_api = [{"role": msg.role, "content": msg.content} for msg in chat_history_db]

            # If user is negotiating a pending income distribution, inject budget constraint into text
            negotiation_context = ""
            if state:
                current_state_check = await state.get_state()
                if current_state_check == IncomeStates.confirming.state:
                    pending_data = await state.get_data()
                    pending_income = pending_data.get("income_amount", 0)
                    if pending_income > 0:
                        negotiation_context = (
                            f"\n\n[SYSTEM: Пользователь обсуждает распределение дохода. "
                            f"Общая сумма дохода = {pending_income:.0f} руб. "
                            f"Твои income_allocations ДОЛЖНЫ СУММИРОВАТЬСЯ РОВНО {pending_income:.0f} руб. "
                            f"Не больше, не меньше. Укажи intent=transaction и income_allocations.]"
                        )

            brain_response = await process_user_message(
                user_text=text + negotiation_context,
                user_vibe=user.prompt_vibe,
                envelopes_context=env_context,
                financial_health=financial_health,
                chat_history=chat_history_api
            )

            existing_real_envs = [
                e for e in envelopes
                if e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")
            ]
            is_first_setup = len(existing_real_envs) == 0

            # Guard: Force LLM intent to transaction if budget already exists
            if brain_response.intent == "profile_update" and not is_first_setup:
                brain_response.intent = "transaction"
                if brain_response.plan_items and not brain_response.income_allocations:
                    brain_response.income_allocations = [
                        IncomeAllocation(envelope_name=p.name, amount=p.amount)
                        for p in brain_response.plan_items
                    ]
                brain_response.plan_items = None
                brain_response.envelopes_to_create = None

            session.add(ChatMessage(user_id=user.telegram_id, role="user", content=text))

            extra_reply_parts = []
            show_envelopes_button = False

            if brain_response.intent == "transaction" and brain_response.transactions:
                for tx_data in brain_response.transactions:
                    if tx_data.action == "expense":
                        target_env = None
                        if tx_data.target_envelope_name:
                            target_env = _find_envelope(envelopes, tx_data.target_envelope_name)
                        if not target_env:
                            target_env = envelopes[0] if envelopes else Envelope(
                                user_id=budget_owner.telegram_id, name="Нераспределённые", current_amount=0
                            )

                        expense_amount = abs(tx_data.amount)
                        new_balance = target_env.current_amount - expense_amount

                        if new_balance < 0:
                            deficit = abs(new_balance)
                            unallocated = _find_unallocated(envelopes)
                            if unallocated and unallocated.current_amount > 0:
                                transfer = min(unallocated.current_amount, deficit)
                                unallocated.current_amount -= transfer
                                target_env.current_amount += transfer
                                remaining_deficit = deficit - transfer
                                if remaining_deficit > 0:
                                    extra_reply_parts.append(
                                        f"⚠️ Статья <b>{target_env.name}</b> в минусе на {remaining_deficit:.0f} руб. "
                                        f"Перенёс {transfer:.0f} руб из Нераспределённых."
                                    )
                                else:
                                    extra_reply_parts.append(
                                        f"ℹ️ Перенёс {transfer:.0f} руб из Нераспределённых в <b>{target_env.name}</b>"
                                    )
                            else:
                                extra_reply_parts.append(
                                    f"🚨 Статья <b>{target_env.name}</b> в минусе ({new_balance:.0f} руб)! "
                                    f"Свободных средств нет."
                                )

                        target_env.current_amount -= expense_amount

                        if target_env.target_amount and target_env.target_amount > 0:
                            pct = target_env.current_amount / target_env.target_amount * 100
                            if pct < 20 and pct > 0:
                                extra_reply_parts.append(
                                    f"⚠️ В статье <b>{target_env.name}</b> осталось меньше 20% ({pct:.0f}%)"
                                )

                        tx = Transaction(
                            user_id=user.telegram_id,
                            amount=-expense_amount,
                            envelope_id=target_env.id,
                            description=tx_data.category or "Трата"
                        )
                        session.add(tx)

                    elif tx_data.action == "income":
                        # Guard: If we are already negotiating this income, don't add it again!
                        current_state_check = await state.get_state() if state else None
                        if current_state_check == IncomeStates.confirming.state:
                            continue

                        unallocated = _find_unallocated(envelopes)
                        if not unallocated:
                            unallocated = Envelope(
                                user_id=budget_owner.telegram_id, name="Нераспределённые", current_amount=0
                            )
                            session.add(unallocated)
                            await session.flush()
                            envelopes.append(unallocated)

                        actual_amount = abs(tx_data.amount)
                        unallocated.current_amount += actual_amount
                        tx = Transaction(
                            user_id=user.telegram_id,
                            amount=actual_amount,
                            envelope_id=unallocated.id,
                            description=tx_data.category or "Доход"
                        )
                        session.add(tx)

                        allocs = brain_response.income_allocations or brain_response.plan_items
                        if allocs and state:
                            alloc_names = [a.envelope_name if hasattr(a, 'envelope_name') else a.name for a in allocs]
                            alloc_amounts = [a.amount for a in allocs]
                            await state.set_state(IncomeStates.confirming)
                            await state.set_data({
                                "income_amount": actual_amount,
                                "unallocated_env_id": unallocated.id,
                                "alloc_names": alloc_names,
                                "alloc_amounts": alloc_amounts
                            })

            elif brain_response.intent == "profile_update" and brain_response.envelopes_to_create:
                # Guard: if user already has a real budget (non-trivial envelopes), 
                # a second profile_update is almost always the LLM making a mistake.
                # We only allow profile_update to OVERWRITE data if there's no prior budget.
                existing_real_envs = [
                    e for e in envelopes
                    if e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")
                ]
                is_first_setup = len(existing_real_envs) == 0

                if not is_first_setup:
                    # LLM triggered profile_update by mistake. Only CREATE new envelopes,
                    # never overwrite existing ones. This prevents budget corruption.
                    for env_data in brain_response.envelopes_to_create:
                        existing = _find_envelope(envelopes, env_data.name)
                        if not existing:
                            new_env = Envelope(
                                user_id=budget_owner.telegram_id,
                                name=env_data.name,
                                target_amount=env_data.target_amount,
                                current_amount=env_data.current_amount,
                                is_debt=env_data.is_debt,
                                is_goal=env_data.is_goal,
                                min_payment=env_data.min_payment
                            )
                            session.add(new_env)
                            envelopes.append(new_env)
                        # Existing envelopes are NOT touched — protect existing state
                    await session.flush()
                else:
                    # First-time setup — allow full creation
                    affected_envs = []
                    for env_data in brain_response.envelopes_to_create:
                        existing = _find_envelope(envelopes, env_data.name)
                        if existing:
                            existing.target_amount = env_data.target_amount
                            existing.current_amount = env_data.current_amount
                            existing.is_debt = env_data.is_debt
                            existing.is_goal = env_data.is_goal
                            existing.min_payment = env_data.min_payment
                            affected_envs.append(existing)
                        else:
                            new_env = Envelope(
                                user_id=budget_owner.telegram_id,
                                name=env_data.name,
                                target_amount=env_data.target_amount,
                                current_amount=env_data.current_amount,
                                is_debt=env_data.is_debt,
                                is_goal=env_data.is_goal,
                                min_payment=env_data.min_payment
                            )
                            session.add(new_env)
                            affected_envs.append(new_env)
                    await session.flush()
                    for ae in affected_envs:
                        if ae not in envelopes:
                            envelopes.append(ae)

                if brain_response.monthly_income:
                    budget_owner.monthly_income = brain_response.monthly_income

                unallocated = _find_unallocated(envelopes)
                if not unallocated:
                    unallocated = Envelope(
                        user_id=budget_owner.telegram_id, name="Нераспределённые", current_amount=0
                    )
                    session.add(unallocated)
                    await session.flush()
                    envelopes.append(unallocated)
                
                # Идемпотентность: перезаписываем кэш, а не прибавляем!
                if brain_response.free_cash is not None:
                    unallocated.current_amount = brain_response.free_cash

                if getattr(brain_response, 'plan_items', None):
                    for pi in brain_response.plan_items:
                        amount = pi.amount
                        if amount <= 0 or unallocated.current_amount < amount:
                            continue
                            
                        target = _find_envelope(envelopes, pi.name)
                        if not target:
                            # Auto-create missing goal/debt
                            is_debt = "долг" in pi.name.lower() or "кредит" in pi.name.lower()
                            is_goal = not is_debt # Treat unknown as goals
                            target = Envelope(
                                user_id=budget_owner.telegram_id,
                                name=pi.name,
                                target_amount=amount, # rough estimate
                                current_amount=0,
                                is_debt=is_debt,
                                is_goal=is_goal
                            )
                            session.add(target)
                            await session.flush()
                            envelopes.append(target)
                            
                        if getattr(target, 'is_debt', False) or getattr(target, 'is_goal', False):
                            target.current_amount += amount
                            unallocated.current_amount -= amount
                            tx = Transaction(
                                user_id=user.telegram_id,
                                amount=amount,
                                envelope_id=target.id,
                                description=f"Стартовое распределение: {pi.name}"
                            )
                            session.add(tx)
                            
                env_count = len([e for e in envelopes if e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")])
                brain_response.coach_reply += f"\n\n📊 Бюджет сформирован ({env_count} категорий)"

            # Post-validation: fix any math errors from LLM
            if brain_response.intent == "profile_update" and brain_response.envelopes_to_create:
                for env_data in brain_response.envelopes_to_create:
                    existing = _find_envelope(envelopes, env_data.name)
                    if existing and existing.target_amount and existing.target_amount > 0:
                        # Expense envelopes: current_amount cannot exceed target
                        if not existing.is_debt and not existing.is_goal:
                            if env_data.current_amount > existing.target_amount:
                                env_data.current_amount = existing.target_amount
                            if env_data.current_amount < 0:
                                env_data.current_amount = 0
                        # Goal envelopes: current_amount cannot exceed target (can't save more than goal)
                        elif existing.is_goal:
                            if env_data.current_amount > existing.target_amount:
                                env_data.current_amount = existing.target_amount
                            if env_data.current_amount < 0:
                                env_data.current_amount = 0
                        # Debt envelopes: current_amount (paid) cannot exceed target (total debt)
                        elif existing.is_debt:
                            if env_data.current_amount > existing.target_amount:
                                env_data.current_amount = existing.target_amount
                            if env_data.current_amount < 0:
                                env_data.current_amount = 0

            brain_response.coach_reply = validate_plan_math(brain_response)

            if extra_reply_parts:
                brain_response.coach_reply += "\n\n" + "\n".join(extra_reply_parts)

            # Ignore LLM's show_dashboard for inline button to avoid spam
            # if brain_response.show_dashboard:
            #     show_envelopes_button = True

            force_dashboard = _is_dashboard_request(text)
            if force_dashboard:
                await session.flush()
                env_result2 = await session.execute(select(Envelope).where(Envelope.user_id == budget_owner.telegram_id))
                fresh_envelopes = list(env_result2.scalars().all())
                brain_response.coach_reply += "\n\n" + build_dashboard(fresh_envelopes, monthly_income=budget_owner.monthly_income or 0)

            session.add(ChatMessage(user_id=user.telegram_id, role="assistant", content=brain_response.coach_reply))
            await session.commit()

        safe_reply = brain_response.coach_reply
        for old, new in [("<br>", "\n"), ("<br/>", "\n"), ("</br>", ""), ("<p>", ""), ("</p>", "\n"),
                         ("<strong>", "<b>"), ("</strong>", "</b>"), ("<em>", "<i>"), ("</em>", "</i>"),
                         ("<ul>", ""), ("</ul>", ""), ("<ol>", ""), ("</ol>", ""),
                         ("<li>", "• "), ("</li>", "\n")]:
            safe_reply = safe_reply.replace(old, new)

        is_profile_update = brain_response.intent == "profile_update"
        allocs = brain_response.income_allocations or (brain_response.plan_items if not is_profile_update else None)
        if allocs and state:
            current_state = await state.get_state()
            alloc_names = [a.envelope_name if hasattr(a, 'envelope_name') else a.name for a in allocs]
            alloc_amounts = [a.amount for a in allocs]
            
            if current_state != IncomeStates.confirming.state:
                # LLM forgot to put action="income" in transactions list. Auto-infer it!
                total_income = sum(a.amount for a in allocs)
                unallocated = _find_unallocated(envelopes)
                if unallocated:
                    unallocated.current_amount += total_income
                    tx = Transaction(
                        user_id=user.telegram_id,
                        amount=total_income,
                        envelope_id=unallocated.id,
                        description="Доход (авто-возобновление)"
                    )
                    session.add(tx)
                    await session.commit()
                    
                    await state.set_state(IncomeStates.confirming)
                    await state.set_data({
                        "income_amount": total_income,
                        "unallocated_env_id": unallocated.id,
                        "alloc_names": alloc_names,
                        "alloc_amounts": alloc_amounts
                    })
            else:
                # Already confirming, update negotiated allocations.
                # CRITICAL: cap total to never exceed the original income amount
                state_data = await state.get_data()
                income_amount_cap = state_data.get("income_amount", 0)
                
                total_proposed = sum(alloc_amounts)
                if total_proposed > income_amount_cap and total_proposed > 0:
                    # Scale down proportionally
                    scale = income_amount_cap / total_proposed
                    alloc_amounts = [round(a * scale / 1000) * 1000 for a in alloc_amounts]
                    # Adjust last item to hit exact total
                    diff = income_amount_cap - sum(alloc_amounts)
                    if alloc_amounts:
                        alloc_amounts[-1] = max(0, alloc_amounts[-1] + diff)
                
                state_data["alloc_names"] = alloc_names
                state_data["alloc_amounts"] = alloc_amounts
                await state.set_data(state_data)

        reply_markup = None
        is_profile_update = brain_response.intent == "profile_update"
        has_allocs = brain_response.income_allocations or (brain_response.plan_items and not is_profile_update)
        if has_allocs and not is_profile_update:
            reply_markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Подтверждаю", callback_data="confirm_income")],
                [InlineKeyboardButton(text="❌ Оставить в Нераспределённых", callback_data="reject_income")]
            ])
        elif force_dashboard:
            reply_markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👥 Семейный бюджет", callback_data="family_menu")]
            ])
        elif show_envelopes_button:
            reply_markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📊 Показать мой бюджет", callback_data="show_envelopes")]
            ])

        # Send final answer first to prevent visual gap
        await message.answer(safe_reply, parse_mode="HTML", reply_markup=reply_markup)
        
        # Clean up loading messages AFTER sending final answer
        if 'animation_task' in locals() and animation_task:
            animation_task.cancel()
            
        if loading_msgs:
            for msg in loading_msgs:
                try:
                    await msg.delete()
                except Exception:
                    pass

    except Exception as e:
        logger.error(f"Error processing transaction: {e}", exc_info=True)
        await message.answer("Блин, не совсем понял. Можешь повторить подробнее?")


@router.callback_query(F.data == "confirm_income", IncomeStates.confirming)
async def confirm_income(callback, state: FSMContext):
    data = await state.get_data()
    income_amount = data.get("income_amount", 0)
    unallocated_env_id = data.get("unallocated_env_id")
    alloc_names = data.get("alloc_names", [])
    alloc_amounts = data.get("alloc_amounts", [])

    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = user_result.scalar_one_or_none()
        
        budget_owner = user
        if user and user.family_host_id:
            host_result = await session.execute(select(User).where(User.telegram_id == user.family_host_id))
            host = host_result.scalar_one_or_none()
            if host:
                budget_owner = host
            else:
                user.family_host_id = None
                await session.flush()

        result = await session.execute(select(Envelope).where(Envelope.id == unallocated_env_id))
        unallocated = result.scalar_one_or_none()

        if not unallocated:
            await callback.message.answer("Счет не найден. Деньги остались в Нераспределённых.")
            await state.clear()
            return

        distribution_text_parts = []
        for name, amount in zip(alloc_names, alloc_amounts):
            if amount <= 0:
                continue
            env_result = await session.execute(
                select(Envelope).where(
                    Envelope.user_id == budget_owner.telegram_id,
                    Envelope.name.ilike(name.strip())
                )
            )
            target_env = env_result.scalar_one_or_none()

            if not target_env:
                is_debt = "долг" in name.lower() or "кредит" in name.lower()
                is_goal = any(w in name.lower() for w in ["отпуск", "подушка", "накоп", "на ", "цель"])
                target_env = Envelope(
                    user_id=budget_owner.telegram_id,
                    name=name,
                    current_amount=0,
                    is_debt=is_debt,
                    is_goal=is_goal
                )
                if is_debt:
                    target_env.target_amount = amount
                session.add(target_env)
                await session.flush()

            # Cap each payment to what's actually available
            target_env.current_amount += amount
            # For debt envelopes, don't overpay beyond remaining balance
            if getattr(target_env, 'is_debt', False) and target_env.target_amount:
                overpaid = target_env.current_amount - target_env.target_amount
                if overpaid > 0:
                    amount -= overpaid
                    target_env.current_amount = target_env.target_amount
            safe_deduction = min(amount, max(0, unallocated.current_amount))
            unallocated.current_amount -= safe_deduction

            tx = Transaction(
                user_id=callback.from_user.id,
                amount=amount,
                envelope_id=target_env.id,
                description=f"Распределение дохода: {name}"
            )
            session.add(tx)
            distribution_text_parts.append(f"• {fmt_money(amount)} → {name}")

        await session.commit()

    reply = f"✅ <b>Распределил {fmt_money(income_amount)}:</b>\n" + "\n".join(distribution_text_parts)
    await callback.message.edit_text(reply, parse_mode="HTML")
    await state.clear()


@router.callback_query(F.data == "reject_income", IncomeStates.confirming)
async def reject_income(callback, state: FSMContext):
    await callback.message.edit_text(
        "👌 Деньги остались в <b>Нераспределённых</b>. Скажи, когда решишь, куда их направить.",
        parse_mode="HTML"
    )
    await state.clear()


@router.callback_query(F.data == "show_envelopes")
async def show_envelopes_callback(callback):
    async with async_session_maker() as session:
        user_result = await session.execute(
            select(User).where(User.telegram_id == callback.from_user.id)
        )
        user = user_result.scalar_one_or_none()
        
        budget_owner = user
        if user and user.family_host_id:
            host_result = await session.execute(select(User).where(User.telegram_id == user.family_host_id))
            host = host_result.scalar_one_or_none()
            if host:
                budget_owner = host
            else:
                user.family_host_id = None
                await session.flush()
        
        env_result = await session.execute(
            select(Envelope).where(Envelope.user_id == budget_owner.telegram_id)
        )
        envelopes = list(env_result.scalars().all())
        monthly_income = budget_owner.monthly_income if budget_owner else 0.0

    if not envelopes:
        await callback.answer("Бюджет пока пуст")
        return

    dashboard = build_dashboard(envelopes, monthly_income=monthly_income)
    reply_markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Семейный бюджет", callback_data="family_menu")]
    ])
    try:
        await callback.message.edit_text(dashboard, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(dashboard, parse_mode="HTML", reply_markup=reply_markup)


@router.message(F.text & ~F.text.startswith('/'))
async def process_transaction_text(message: Message, state: FSMContext):
    await message.chat.do("typing")
    await handle_transaction(message, message.text, state=state)

import os

@router.message(F.voice)
async def process_voice(message: Message, bot: Bot, state: FSMContext = None):
    await message.chat.do("typing")
    file_path = f"temp_voice_{message.from_user.id}_{message.message_id}.ogg"
    try:
        file = await bot.get_file(message.voice.file_id)
        await bot.download_file(file.file_path, destination=file_path)

        if not os.path.exists(file_path):
            raise ValueError("Failed to save file from Telegram")

        transcribed_text = await transcribe_voice(file_path)
        await message.answer(f"🎤 <i>{transcribed_text}</i>", parse_mode="HTML")
        await handle_transaction(message, transcribed_text, state=state)

    except Exception as e:
        logger.error(f"Error processing voice: {e}")
        await message.answer("Ой, не удалось распознать голосовое. Давай лучше текстом?")
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
