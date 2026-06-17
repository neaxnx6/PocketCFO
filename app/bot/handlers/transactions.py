import logging
from datetime import datetime
from typing import Optional
from aiogram import Router, F, Bot
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.future import select
from sqlalchemy import func, delete

from app.database.session import async_session_maker
from app.database.models import User, Transaction, Envelope, ChatMessage
from app.database.query_helpers import get_monthly_payments, get_monthly_spending
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
    "📊 навигатор", "🛍 расходы", "💳 долги",
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
    confirming_paid_limit = State()


async def verify_user_ledger(session, user_id: int) -> bool:
    """
    Enforce database consistency invariants:
    1. Sum of all envelopes (current_amount) == Sum of all transactions (amount)
    2. Sum of transactions for each envelope == envelope.current_amount
    Returns True if ledger is consistent, False otherwise.
    """
    try:
        await session.flush()
        result_envs = await session.execute(select(Envelope).where(Envelope.user_id == user_id))
        envelopes = list(result_envs.scalars().all())
        
        env_ids = [e.id for e in envelopes]
        if not env_ids:
            return True
            
        result_txs = await session.execute(
            select(Transaction).where(
                (Transaction.user_id == user_id) | Transaction.envelope_id.in_(env_ids)
            )
        )
        transactions = list(result_txs.scalars().all())
        
        sum_envelopes = sum(e.current_amount for e in envelopes)
        
        # Deduplicate transactions if any overlap in filters
        seen_tx_ids = set()
        unique_transactions = []
        for t in transactions:
            if t.id not in seen_tx_ids:
                seen_tx_ids.add(t.id)
                unique_transactions.append(t)
                
        sum_transactions = sum(t.amount for t in unique_transactions)
        
        if abs(sum_envelopes - sum_transactions) > 0.01:
            logger.error(
                f"Ledger invariant violation for user {user_id}: "
                f"envelopes sum = {sum_envelopes:.2f}, transactions sum = {sum_transactions:.2f}"
            )
            return False
            
        for env in envelopes:
            env_tx_sum = sum(t.amount for t in unique_transactions if t.envelope_id == env.id)
            if abs(env.current_amount - env_tx_sum) > 0.01:
                logger.error(
                    f"Envelope balance drift for user {user_id}, envelope '{env.name}' (id={env.id}): "
                    f"current_amount = {env.current_amount:.2f}, transactions sum = {env_tx_sum:.2f}"
                )
                return False
                
        return True
    except Exception as e:
        logger.error(f"Error during ledger verification: {e}", exc_info=True)
        return False


def _normalize_name(name: str) -> str:
    """Normalize envelope name for matching: remove prefixes, lowercase, strip."""
    name = name.lower().strip()
    # Remove common prefixes that shouldn't affect matching
    for prefix in ["кредитка ", "долг ", "карта ", "долг маме ", "долг сестре "]:
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name.strip()


CANONICAL_ENVELOPE_MAPPING = {
    # Жилье
    "аренда": "Жилье", "коммуналка": "Жилье", "интернет": "Жилье", "связь": "Жилье", 
    "жкх": "Жилье", "жилье": "Жилье", "жильё": "Жилье", "квартира": "Жилье", "дом": "Жилье",
    "электричество": "Жилье", "отопление": "Жилье", "вода": "Жилье", "телефон": "Жилье",
    "мобильный": "Жилье", "жк": "Жилье",
    
    # Еда
    "продукты": "Еда", "еда": "Еда", "супермаркет": "Еда", "кафе": "Еда", "ресторан": "Еда", 
    "доставка": "Еда", "фастфуд": "Еда", "кофе": "Еда", "кофейня": "Еда",
    
    # Транспорт
    "машина": "Транспорт", "бензин": "Транспорт", "такси": "Транспорт", "проезд": "Транспорт", 
    "метро": "Транспорт", "каршеринг": "Транспорт", "автобус": "Транспорт", "поезд": "Транспорт", 
    "парковка": "Транспорт", "авто": "Транспорт", "топливо": "Транспорт",
    
    # Личное
    "одежда": "Личное", "красота": "Личное", "хобби": "Личное", "развлечения": "Личное", 
    "подарки": "Личное", "спорт": "Личное", "здоровье": "Личное", "аптека": "Личное", 
    "кино": "Личное", "шопинг": "Личное", "косметика": "Личное", "подписка": "Личное", "салон": "Личное"
}


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
    # Then try canonical mapping (synonyms)
    canonical = CANONICAL_ENVELOPE_MAPPING.get(name_normalized)
    if canonical:
        canonical_norm = _normalize_name(canonical)
        for env in envelopes:
            if _normalize_name(env.name) == canonical_norm:
                return env
    return None


def _find_unallocated(envelopes: list):
    for env in envelopes:
        if env.name.lower().strip() in ("нераспределённые", "кошелек", "кошелёк"):
            return env
    return None


def build_envelopes_context(envelopes: list[Envelope], monthly_payments: dict[int, float] = None) -> str:
    expense_lines = []
    goal_lines = []
    debt_lines = []
    for e in envelopes:
        if getattr(e, 'is_debt', False):
            remaining = (e.target_amount or 0) - e.current_amount
            min_pay_str = ""
            if e.min_payment:
                paid_this_month = monthly_payments.get(e.id, 0.0) if monthly_payments else 0.0
                min_pay_str = f", мин. платёж {e.min_payment:.0f} руб (внесено в этом месяце {paid_this_month:.0f} руб)"
            debt_lines.append(
                f"- [ДОЛГ] '{e.name}': осталось вернуть {remaining:.0f} руб "
                f"(оплачено {e.current_amount:.0f} из {e.target_amount or 0:.0f}){min_pay_str}"
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


def get_days_left(due_day: Optional[int], current_day: int) -> int:
    if due_day is None:
        return 999
    if due_day >= current_day:
        return due_day - current_day
    return 30 + due_day - current_day


def get_sorting_priority(e, current_day: int, monthly_payments: dict, monthly_spending: dict) -> int:
    due_day = getattr(e, 'due_day', None)
    if due_day is None:
        return 999
        
    current_month_str = datetime.utcnow().strftime("%Y-%m")
    if getattr(e, 'last_paid_month', None) == current_month_str:
        return 999

    if getattr(e, 'is_debt', False):
        paid = monthly_payments.get(e.id, 0.0)
        min_pay = getattr(e, 'min_payment', 0.0) or 0.0
        is_paid = paid >= min_pay
    else:
        spent = monthly_spending.get(e.id, 0.0)
        target = getattr(e, 'target_amount', 0.0) or 0.0
        is_paid = (getattr(e, 'current_amount', 0.0) + spent) >= target
        
    if is_paid:
        return 999
        
    if current_day > due_day:
        return -100 - (current_day - due_day)
        
    return due_day - current_day


def get_envelope_due_status_str(e, spent_this_month: float, current_day: int) -> str:
    current_month_str = datetime.utcnow().strftime("%Y-%m")
    if getattr(e, 'last_paid_month', None) == current_month_str:
        return "Оплачено в этом месяце ✅"

    due_day = getattr(e, 'due_day', None)
    if getattr(e, 'is_debt', False):
        min_pay = getattr(e, 'min_payment', 0.0) or 0.0
        if min_pay > 0:
            if spent_this_month >= min_pay:
                return "Оплачено в этом месяце ✅"
            else:
                if due_day is None:
                    return ""
                if current_day < due_day:
                    return f"До оплаты {due_day - current_day} дн. ⏰"
                elif current_day == due_day:
                    return "Срок оплаты сегодня! ⚠️"
                else:
                    return f"Просрочено на {current_day - due_day} дн. 🔴"
        return ""
    else:
        target = getattr(e, 'target_amount', 0.0) or 0.0
        if target <= 0:
            return ""
        funded = getattr(e, 'current_amount', 0.0) + spent_this_month
        if funded >= target:
            if spent_this_month > 0:
                return "Оплачено в этом месяце ✅"
            else:
                return "🟢 Обеспечено"
        else:
            if due_day is None:
                return ""
            if current_day < due_day:
                return f"До оплаты {due_day - current_day} дн. ⏰"
            elif current_day == due_day:
                return "Срок оплаты сегодня! ⚠️"
            else:
                return f"Просрочено на {current_day - due_day} дн. 🔴"


def get_health_status(envelopes: list, monthly_payments: dict = None, monthly_spending: dict = None) -> str:
    monthly_payments = monthly_payments or {}
    monthly_spending = monthly_spending or {}
    current_day = datetime.utcnow().day
    
    debt_envs = [e for e in envelopes if getattr(e, 'is_debt', False)]
    expense_envs = [
        e for e in envelopes 
        if not getattr(e, 'is_debt', False) 
        and not getattr(e, 'is_goal', False) 
        and "буфер" not in e.name.lower() 
        and e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")
    ]
    
    # Check for Level 0 - Overdue (Пожар)
    current_month_str = datetime.utcnow().strftime("%Y-%m")
    overdue_envs = []
    for e in expense_envs:
        if getattr(e, 'last_paid_month', None) == current_month_str:
            continue
        due_day = getattr(e, 'due_day', None)
        if due_day is not None:
            target = getattr(e, 'target_amount', 0.0) or 0.0
            spent = monthly_spending.get(e.id, 0.0)
            funded = getattr(e, 'current_amount', 0.0) + spent
            if funded < target and current_day > due_day:
                overdue_envs.append(e)
                
    for d in debt_envs:
        if getattr(d, 'last_paid_month', None) == current_month_str:
            continue
        rem = (getattr(d, 'target_amount', 0.0) or 0.0) - getattr(d, 'current_amount', 0.0)
        due_day = getattr(d, 'due_day', None)
        if rem > 0 and due_day is not None:
            min_pay = getattr(d, 'min_payment', 0.0) or 0.0
            paid = monthly_payments.get(d.id, 0.0)
            if paid < min_pay and current_day > due_day:
                overdue_envs.append(d)
                
    if overdue_envs:
        overdue_names = ", ".join(e.name for e in overdue_envs)
        return f"🚨 <b>Состояние:</b> Пожар (есть просрочки: {overdue_names})"

    # Check for Level 1 - Base expenses (Выживание)
    underfunded_base = []
    for e in expense_envs:
        if get_envelope_group(e.name) in ("🏠 Жилье", "🍔 Еда", "🚗 Транспорт"):
            target = e.target_amount or 0.0
            spent = monthly_spending.get(e.id, 0.0)
            funded = e.current_amount + spent
            if funded < target:
                underfunded_base.append(e)
                
    if underfunded_base:
        return "🟡 <b>Состояние:</b> Выживание (базовые расходы не обеспечены)"

    # Check for Level 2 - Minimum debt payments (Минимальные платежи)
    underfunded_mins = []
    for d in debt_envs:
        rem = (d.target_amount or 0.0) - d.current_amount
        if rem > 0:
            min_pay = d.min_payment or 0.0
            paid = monthly_payments.get(d.id, 0.0)
            if paid < min_pay:
                underfunded_mins.append(d)
                
    if underfunded_mins:
        return "🟡 <b>Состояние:</b> Минимальные платежи (нужно внести мин. платежи)"

    # Check for Level 3 - Cushion (Подушка)
    buffer_env = next((e for e in envelopes if "буфер" in e.name.lower()), None)
    buffer_target = buffer_env.target_amount or 30000.0 if buffer_env else 30000.0
    buffer_val = buffer_env.current_amount if buffer_env else 0.0
    if buffer_val < buffer_target:
        return "🟡 <b>Состояние:</b> Подушка (все обязательства закрыты, собираем резерв)"

    # Level 4 - Growth (Цели и досрочное погашение)
    return "🟢 <b>Состояние:</b> Рост (цели и ускоренное погашение)"


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
            "initial": (d.target_amount or 0) - d.current_amount,
            "remaining": (d.target_amount or 0) - d.current_amount,
            "is_debt": True
        })
    for g in active_goals:
        sim_items.append({
            "name": g.name,
            "initial": (g.target_amount or 0) - g.current_amount,
            "remaining": (g.target_amount or 0) - g.current_amount,
            "is_debt": False
        })

    completed = {}
    current_month = 0
    max_months = 120
    end_of_first_month_debts = []
    
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
        
        if current_month == 1:
            for item in sim_items:
                if item["is_debt"]:
                    end_of_first_month_debts.append({
                        "name": item["name"],
                        "initial": item["initial"],
                        "remaining": item["remaining"]
                    })

    return {
        "status": "ok",
        "free_cash": free_cash,
        "completed": completed,
        "end_of_first_month_debts": end_of_first_month_debts
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


def get_envelope_group(name: str) -> str:
    n = name.lower()
    # 🏠 Жилье: аренда, коммуналка, интернет, связь, жкх, жилье, квартира, дом, жк, электричество, отопление, вода, газ
    if any(k in n for k in ("аренда", "коммуналка", "интернет", "связь", "жкх", "жильё", "жилье", "квартира", "дом", "жк", "электричеств", "отоплен", "телефон", "мобильн", "провайдер")):
        if "офис" in n or "кабинет" in n:
            return "📦 Прочее"
        return "🏠 Жилье"
        
    # 🍔 Еда: еда, продукты, кафе, ресторан, доставка, фастфуд, супермаркет, макдональдс, пицца, суши, кофейня, кофе
    if any(k in n for k in ("еда", "продукты", "кафе", "ресторан", "доставка", "фастфуд", "супермаркет", "пицца", "суши", "кофе")):
        return "🍔 Еда"
        
    # 🚗 Транспорт: машина, бензин, такси, проезд, метро, каршеринг, автобус, поезд, парковка, авто, топливо, гараж
    if any(k in n for k in ("машина", "бензин", "такси", "проезд", "метро", "каршеринг", "автобус", "поезд", "парковка", "авто", "топливо", "гараж")):
        return "🚗 Транспорт"
        
    # ❤️ Личное: одежда, красота, хобби, развлечения, подарки, спорт, здоровье, аптека, кино, шоппинг, шопинг, косметика, подписка, книга
    if any(k in n for k in ("одежда", "красота", "хобби", "развлеч", "подарки", "спорт", "здоровье", "аптека", "кино", "шопинг", "шоппинг", "косметик", "подписк", "книг", "личное", "салон")):
        return "❤️ Личное"
        
    # 📦 Прочее: default
    return "📦 Прочее"


def get_financial_insight(envelopes: list, monthly_payments: dict = None, monthly_spending: dict = None) -> str:
    monthly_payments = monthly_payments or {}
    monthly_spending = monthly_spending or {}
    current_day = datetime.utcnow().day

    expense_envs = [
        e for e in envelopes 
        if not getattr(e, 'is_debt', False) 
        and not getattr(e, 'is_goal', False) 
        and "буфер" not in e.name.lower()
        and e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")
    ]
    debt_envs = [e for e in envelopes if getattr(e, 'is_debt', False)]
    goal_envs = [e for e in envelopes if getattr(e, 'is_goal', False) and "буфер" not in e.name.lower()]
    buffer_env = next((e for e in envelopes if "буфер" in e.name.lower()), None)

    # Check for overdue envelopes (Level 0 - Пожар)
    current_month_str = datetime.utcnow().strftime("%Y-%m")
    overdue_envs = []
    for e in expense_envs:
        if getattr(e, 'last_paid_month', None) == current_month_str:
            continue
        due_day = getattr(e, 'due_day', None)
        if due_day is not None:
            target = getattr(e, 'target_amount', 0.0) or 0.0
            spent = monthly_spending.get(e.id, 0.0)
            funded = getattr(e, 'current_amount', 0.0) + spent
            if funded < target and current_day > due_day:
                overdue_envs.append(e)
                
    for d in debt_envs:
        if getattr(d, 'last_paid_month', None) == current_month_str:
            continue
        rem_debt = (getattr(d, 'target_amount', 0.0) or 0.0) - getattr(d, 'current_amount', 0.0)
        due_day = getattr(d, 'due_day', None)
        if rem_debt > 0 and due_day is not None:
            min_pay = getattr(d, 'min_payment', 0.0) or 0.0
            paid = monthly_payments.get(d.id, 0.0)
            if paid < min_pay and current_day > due_day:
                overdue_envs.append(d)

    # Underfunded Base (Level 1 - Жилье, Еда, Транспорт)
    underfunded_base = []
    # Underfunded Other (Level 3 - Прочие)
    underfunded_other = []
    
    for e in expense_envs:
        if getattr(e, 'last_paid_month', None) == current_month_str:
            needed = 0.0
        else:
            spent = monthly_spending.get(e.id, 0.0)
            needed = max(0.0, (e.target_amount or 0.0) - (e.current_amount + spent))
        if needed > 0:
            if get_envelope_group(e.name) in ("🏠 Жилье", "🍔 Еда", "🚗 Транспорт"):
                underfunded_base.append(e)
            else:
                underfunded_other.append(e)

    # Underfunded Mins (Level 2 - Мин. платежи по кредитам)
    underfunded_mins = []
    active_debts = []
    for d in debt_envs:
        rem_debt = (d.target_amount or 0.0) - d.current_amount
        if getattr(d, 'last_paid_month', None) == current_month_str:
            rem_debt = 0.0
        if rem_debt > 0:
            active_debts.append(d)
            min_pay = d.min_payment or 0.0
            if min_pay > 0:
                paid = monthly_payments.get(d.id, 0.0)
                needed = max(0.0, min_pay - paid)
                needed = min(needed, rem_debt)
                if needed > 0:
                    underfunded_mins.append((d, needed))

    buffer_target = buffer_env.target_amount or 30000.0 if buffer_env else 30000.0
    buffer_val = buffer_env.current_amount if buffer_env else 0.0
    buffer_needed = max(0.0, buffer_target - buffer_val)
    underfunded_goals = [g for g in goal_envs if (g.target_amount or 0.0) - g.current_amount > 0]

    header = ""
    desc = ""
    reason = ""

    if overdue_envs:
        # Level 0
        total_overdue = 0.0
        overdue_list = []
        for e in overdue_envs:
            if getattr(e, 'is_debt', False):
                paid = monthly_payments.get(e.id, 0.0)
                needed = max(0.0, (e.min_payment or 0.0) - paid)
                needed = min(needed, (e.target_amount or 0.0) - e.current_amount)
                total_overdue += needed
                overdue_list.append(f"{e.name} ({fmt_money(needed)})")
            else:
                spent = monthly_spending.get(e.id, 0.0)
                needed = max(0.0, (e.target_amount or 0.0) - (e.current_amount + spent))
                total_overdue += needed
                overdue_list.append(f"{e.name} ({fmt_money(needed)})")
        
        header = "🚨 <b>СРОЧНЫЙ ПОЖАР</b>"
        desc = f"У тебя есть просроченные платежи на сумму <b>{fmt_money(total_overdue)}</b>: {', '.join(overdue_list)}."
        reason = "Просрочка — это приоритет №1. Срочно внеси эти платежи, чтобы избежать штрафов, пени и ухудшения кредитной истории."
    elif underfunded_base:
        # Level 1
        total_base_deficit = sum(max(0.0, (e.target_amount or 0.0) - (e.current_amount + monthly_spending.get(e.id, 0.0))) for e in underfunded_base)
        header = "🔥 <b>Следующий шаг (Выживание)</b>"
        desc = f"Для спокойного прохождения месяца пока не хватает <b>{fmt_money(total_base_deficit)}</b> на жилье, еду и транспорт."
        reason = "Сначала закрываем базовые нужды (жилье, еду, транспорт), чтобы обеспечить безопасность. После этого перейдем к обязательным платежам по кредитам."
    elif underfunded_mins:
        # Level 2
        total_min_deficit = sum(needed for d, needed in underfunded_mins)
        header = "🔥 <b>Следующий шаг (Минимальные платежи)</b>"
        desc = f"Базовые нужды закрыты. Теперь нужно обеспечить минимальные платежи по кредитам на сумму <b>{fmt_money(total_min_deficit)}</b>, чтобы избежать штрафов."
        reason = "Базовые нужды закрыты. Теперь закрываем обязательные платежи по кредитам, чтобы избежать штрафов и пени."
    elif underfunded_other:
        total_other_deficit = sum(max(0.0, (e.target_amount or 0.0) - (e.current_amount + monthly_spending.get(e.id, 0.0))) for e in underfunded_other)
        header = "💡 <b>Следующий шаг (Месячные расходы)</b>"
        desc = f"Осталось закрыть прочие расходы месяца на сумму <b>{fmt_money(total_other_deficit)}</b>."
        reason = "Все обязательные платежи и базовые расходы обеспечены. Направляем деньги на прочие запланированные покупки месяца."
    elif buffer_needed > 0:
        header = "🛡 <b>Следующий шаг (Подушка)</b>"
        desc = f"Все обязательства обеспечены! Рекомендую собрать резерв безопасности (цель: <b>{fmt_money(buffer_target)}</b>, не хватает <b>{fmt_money(buffer_needed)}</b>)."
        reason = "Обязательства закрыты! Направляем деньги на создание буфера безопасности, чтобы застраховаться от форс-мажоров."
    else:
        # Level 4
        if active_debts:
            active_debts.sort(key=lambda x: (x.target_amount or 0.0) - x.current_amount)
            smallest_debt = active_debts[0]
            rem = (smallest_debt.target_amount or 0.0) - smallest_debt.current_amount
            header = "🚀 <b>Фокус (Ускоренное погашение)</b>"
            desc = f"Все обязательства и буфер закрыты! Свободные деньги направляй на досрочное закрытие долга «{smallest_debt.name}» (осталось <b>{fmt_money(rem)}</b>)."
            reason = "Все базовые потребности и буфер закрыты! Свободные деньги направляем на досрочное закрытие кредитов (метод снежного кома), чтобы сэкономить на процентах."
        elif underfunded_goals:
            header = "🎯 <b>Фокус (Цели)</b>"
            desc = "Бюджет сбалансирован. Все свободные деньги теперь работают на твои цели!"
            reason = "Бюджет в идеальном балансе. Направляй свободные деньги на крупные цели."
        else:
            header = "💡 <b>Фокус</b>"
            desc = "Бюджет в норме. Распределяй новые доходы по правилу: сначала плати себе (буфер), потом трать."
            reason = "Все базовые потребности закрыты. Свободные деньги можно направить на новые цели или инвестиции."

    # Build queue list
    queue_items = []
    # 1. Base
    underfunded_base_sorted = sorted(underfunded_base, key=lambda x: get_sorting_priority(x, current_day, monthly_payments, monthly_spending))
    for e in underfunded_base_sorted:
        spent = monthly_spending.get(e.id, 0.0)
        needed = max(0.0, (e.target_amount or 0.0) - (e.current_amount + spent))
        queue_items.append((e.name, needed))
        
    # 2. Min payments
    underfunded_mins_sorted = sorted(underfunded_mins, key=lambda x: get_sorting_priority(x[0], current_day, monthly_payments, monthly_spending))
    for d, needed in underfunded_mins_sorted:
        queue_items.append((f"{d.name} (мин. платёж)", needed))
        
    # 3. Other
    underfunded_other_sorted = sorted(underfunded_other, key=lambda x: get_sorting_priority(x, current_day, monthly_payments, monthly_spending))
    for e in underfunded_other_sorted:
        spent = monthly_spending.get(e.id, 0.0)
        needed = max(0.0, (e.target_amount or 0.0) - (e.current_amount + spent))
        queue_items.append((e.name, needed))
        
    # 4. Buffer
    if buffer_needed > 0:
        queue_items.append(("Буфер безопасности", buffer_needed))
        
    # 5. Goals
    underfunded_goals_sorted = sorted(underfunded_goals, key=lambda x: get_sorting_priority(x, current_day, monthly_payments, monthly_spending))
    for g in underfunded_goals_sorted:
        needed = max(0.0, (g.target_amount or 0.0) - g.current_amount)
        queue_items.append((g.name, needed))

    # Remove duplicates
    seen = set()
    unique_items = []
    for name, needed in queue_items:
        if name not in seen and needed > 0:
            seen.add(name)
            unique_items.append((name, needed))

    if unique_items:
        # Deficit block
        deficit_lines = []
        for name, needed in unique_items[:4]:  # limit to top 4
            deficit_lines.append(f"• {name} (не хватает <b>{fmt_money(needed)}</b>)")
        deficit_text = "<b>Очередь финансирования:</b>\n" + "\n".join(deficit_lines)

        # Plan of action
        action_lines = []
        if len(unique_items) >= 1:
            action_lines.append(f"• первые <b>{fmt_money(unique_items[0][1])}</b> → {unique_items[0][0]}")
        if len(unique_items) >= 2:
            action_lines.append(f"• следующие <b>{fmt_money(unique_items[1][1])}</b> → {unique_items[1][0]}")
        if len(unique_items) >= 3:
            action_lines.append(f"• затем → {unique_items[2][0]}")
            
        action_text = "<b>Если придут следующие деньги:</b>\n" + "\n".join(action_lines)

        return f"{header}\n{desc}\n\n<i>{reason}</i>\n\n{deficit_text}\n\n{action_text}"
    else:
        return f"{header}\n{desc}\n\n<i>{reason}</i>"


def build_micro_navigator(
    envelopes: list, 
    transactions: list, 
    monthly_payments: dict = None,
    pending_allocation_amount: float = 0.0
) -> str:
    lines = []
    
    unallocated = _find_unallocated(envelopes)
    unallocated_amt = unallocated.current_amount if unallocated else 0.0
    if pending_allocation_amount > 0:
        remaining = max(0.0, unallocated_amt - pending_allocation_amount)
        lines.append(f"💰 <b>Свободный кэш:</b> <b>{fmt_money(unallocated_amt)}</b> (после распределения останется <b>{fmt_money(remaining)}</b>)")
    else:
        lines.append(f"💰 <b>Свободный кэш:</b> <b>{fmt_money(unallocated_amt)}</b>")
    
    for tx in transactions:
        if not tx.target_envelope_name:
            continue
        env = _find_envelope(envelopes, tx.target_envelope_name)
        if not env:
            continue
            
        # Skip redundant display for the unallocated wallet itself
        if env.name.lower().strip() in ("нераспределённые", "кошелек", "кошелёк"):
            continue
            
        if getattr(env, 'is_debt', False):
            if (env.min_payment or 0) > 0:
                paid_this_month = monthly_payments.get(env.id, 0.0) if monthly_payments else 0.0
                paid_min = min(paid_this_month, env.min_payment)
                lines.append(f"💳 <b>«{env.name}» (мин. платёж):</b> оплачено <b>{fmt_money(paid_min)}</b> из <b>{fmt_money(env.min_payment)}</b>")
        elif getattr(env, 'is_goal', False):
            target_str = f" из <b>{fmt_money(env.target_amount)}</b>" if env.target_amount else ""
            lines.append(f"🎯 <b>«{env.name}»:</b> накоплено <b>{fmt_money(env.current_amount)}</b>{target_str}")
        else:
            limit_str = f" из <b>{fmt_money(env.target_amount)}</b>" if env.target_amount else ""
            lines.append(f"🛍 <b>Лимит «{env.name}»:</b> осталось <b>{fmt_money(env.current_amount)}</b>{limit_str}")
            
    return "\n📊 <b>Микро-Навигатор:</b>\n" + "\n".join(lines)


def build_dashboard(
    envelopes: list, 
    monthly_income: float = 0.0, 
    tab: str = 'navigator', 
    monthly_payments: dict = None,
    monthly_spending: dict = None
) -> str:
    # Filter out categories
    expense_envs = [
        e for e in envelopes 
        if not getattr(e, 'is_debt', False) 
        and not getattr(e, 'is_goal', False) 
        and "буфер" not in e.name.lower()
        and e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")
    ]
    debt_envs = [e for e in envelopes if getattr(e, 'is_debt', False)]
    goal_envs = [e for e in envelopes if getattr(e, 'is_goal', False) and "буфер" not in e.name.lower()]
    buffer_env = next((e for e in envelopes if "буфер" in e.name.lower()), None)
    
    monthly_payments = monthly_payments or {}
    monthly_spending = monthly_spending or {}
    current_day = datetime.utcnow().day

    unallocated_amount = 0.0
    for e in envelopes:
        if e.name.lower().strip() in ("нераспределённые", "кошелек", "кошелёк"):
            unallocated_amount = e.current_amount
            
    real_env_count = len(expense_envs) + len(debt_envs) + len(goal_envs) + (1 if buffer_env else 0)
    if real_env_count == 0:
        return "Твой бюджет пока пуст. Расскажи, сколько зарабатываешь, какие есть обязательные расходы и долги, и я составлю финансовый план! 🚀"

    parts = []
    
    # === ВКЛАДКА 1: НАВИГАТОР ===
    if tab == 'navigator':
        expenses_obligations = sum(e.target_amount or 0.0 for e in expense_envs)
        
        debts_obligations = 0.0
        funded_debts = 0.0
        current_month_str = datetime.utcnow().strftime("%Y-%m")
        for d in debt_envs:
            rem_debt = (d.target_amount or 0.0) - d.current_amount
            paid = monthly_payments.get(d.id, 0.0)
            if rem_debt > 0 or paid > 0:
                min_pay = d.min_payment or 0.0
                effective_obligation = min(min_pay, max(0.0, rem_debt + paid))
                debts_obligations += effective_obligation
                
                is_marked_paid = (getattr(d, 'last_paid_month', None) == current_month_str)
                if is_marked_paid:
                    funded_debts += effective_obligation
                else:
                    funded_debts += min(effective_obligation, paid)
                
        total_obligations = expenses_obligations + debts_obligations
        
        funded_expenses = 0.0
        for e in expense_envs:
            target = e.target_amount or 0.0
            spent = monthly_spending.get(e.id, 0.0)
            is_marked_paid = (getattr(e, 'last_paid_month', None) == current_month_str)
            if is_marked_paid:
                funded_expenses += target
            else:
                funded_expenses += min(target, max(0.0, e.current_amount + spent))
            
        total_funded_in_envelopes = funded_expenses + funded_debts
        total_funded = total_funded_in_envelopes + unallocated_amount
        if total_obligations > 0:
            total_funded = min(total_funded, total_obligations)
            
        deficit = max(0.0, total_obligations - total_funded)
        
        coverage_pct = int((total_funded / total_obligations * 100)) if total_obligations > 0 else 100
        
        parts.append("📍 <b>ВКЛАДКА: НАВИГАТОР</b>\n")
        parts.append(f"📊 <b>Обеспеченность месяца:</b> <b>{coverage_pct}%</b>")
        parts.append(get_health_status(envelopes, monthly_payments, monthly_spending))
        parts.append("")  # Empty line
        
        total_in_envelopes = sum(e.current_amount for e in expense_envs) + sum(e.current_amount for e in goal_envs)
        buffer_amount = buffer_env.current_amount if buffer_env else 0.0
        total_money = total_in_envelopes + unallocated_amount + buffer_amount
        
        free_cash_line = f"• Свободно: <b>{fmt_money(unallocated_amount)}</b>"
        if unallocated_amount > 0 and deficit > 0:
            if unallocated_amount >= deficit:
                free_cash_line += " (этого достаточно для полного покрытия месяца, осталось распределить! ✨)"
            else:
                free_cash_line += f" (хватит, чтобы покрыть еще <b>{fmt_money(unallocated_amount)}</b> обязательств 💡)"
                
        parts.append(
            f"💰 <b>Деньги:</b> <b>{fmt_money(total_money)}</b>\n"
            f"{free_cash_line}\n"
            f"• В конвертах: <b>{fmt_money(total_in_envelopes)}</b>\n"
            f"• Буфер: <b>{fmt_money(buffer_amount)}</b>"
        )
        parts.append("")
        
        parts.append(
            f"💳 <b>ОБЯЗАТЕЛЬСТВА:</b> <b>{fmt_money(total_obligations)}</b>\n"
            f"• Расходы: <b>{fmt_money(expenses_obligations)}</b>\n"
            f"• Мин. платежи: <b>{fmt_money(debts_obligations)}</b>\n"
            f"• Обеспечено: <b>{fmt_money(total_funded)}</b>\n"
            f"• Не хватает: <b>{fmt_money(deficit)}</b>"
        )
        parts.append("")
        
        insight = get_financial_insight(envelopes, monthly_payments, monthly_spending)
        if insight:
            parts.append(insight)

    # === ВКЛАДКА 2: РАСХОДЫ ===
    elif tab == 'expenses':
        parts.append("📍 <b>ВКЛАДКА: РАСХОДЫ</b>\n")
        
        if expense_envs:
            groups = {
                "🏠 Жилье": [],
                "🍔 Еда": [],
                "🚗 Транспорт": [],
                "❤️ Личное": [],
                "📦 Прочее": []
            }
            for e in expense_envs:
                grp = get_envelope_group(e.name)
                groups[grp].append(e)
                
            parts.append("🛍 <b>Расходы по категориям:</b>")
            for grp_name, envs in groups.items():
                if not envs:
                    continue
                grp_available = sum(e.current_amount for e in envs)
                grp_limit = sum(e.target_amount or 0.0 for e in envs)
                
                limit_str = f" из <b>{fmt_money(grp_limit)}</b>" if grp_limit > 0 else ""
                parts.append(f"• {grp_name}: доступно <b>{fmt_money(grp_available)}</b>{limit_str}")
            
        if goal_envs:
            goal_lines = []
            for e in goal_envs:
                target_str = f" (цель <b>{fmt_money(e.target_amount or 0)}</b>)" if (e.target_amount or 0) > 0 else ""
                goal_lines.append(f"• {e.name}: накоплено <b>{fmt_money(e.current_amount)}</b>{target_str}")
            parts.append("\n🎯 <b>Цели и накопления:</b>\n" + "\n".join(goal_lines))
            
        active_debts_with_min = [d for d in debt_envs if (d.min_payment or 0) > 0 and (d.target_amount or 0) - d.current_amount > 0]
        if active_debts_with_min:
            min_pay_lines = []
            for d in active_debts_with_min:
                paid_this_month = monthly_payments.get(d.id, 0.0) if monthly_payments else 0.0
                paid_min = min(paid_this_month, d.min_payment)
                status = get_envelope_due_status_str(d, paid_this_month, current_day)
                status_suffix = f" ({status})" if status else ""
                due_str = f" (до {d.due_day}-го)" if d.due_day else ""
                min_pay_lines.append(f"• {d.name}{due_str} (обязательный платеж): оплачено <b>{fmt_money(paid_min)}</b> из <b>{fmt_money(d.min_payment)}</b>{status_suffix}")
            parts.append("\n💳 <b>Обязательные платежи по кредитам:</b>\n" + "\n".join(min_pay_lines))

    # === ВКЛАДКА 3: ДОЛГИ ===
    elif tab == 'debts':
        parts.append("📍 <b>ВКЛАДКА: ДОЛГИ</b>\n")
        
        active_credits = [d for d in debt_envs if (d.min_payment or 0) > 0 and (d.target_amount or 0) - d.current_amount > 0]
        if active_credits:
            credit_lines = []
            for e in active_credits:
                rem = (e.target_amount or 0) - e.current_amount
                pct = int((e.current_amount / e.target_amount * 100)) if e.target_amount else 0
                pct_str = f", погашено <b>{pct}%</b>" if pct >= 10 else ""
                paid_this_month = monthly_payments.get(e.id, 0.0) if monthly_payments else 0.0
                status = get_envelope_due_status_str(e, paid_this_month, current_day)
                status_suffix = f" — <i>{status}</i>" if status else ""
                due_str = f" (до {e.due_day}-го)" if e.due_day else ""
                credit_lines.append(f"• {e.name}{due_str}: осталось <b>{fmt_money(rem)}</b> (мин. платёж <b>{fmt_money(e.min_payment)}</b>{pct_str}){status_suffix}")
            parts.append("🏦 <b>Банковские кредиты и карты:</b>\n" + "\n".join(credit_lines))
            
        active_personal = [d for d in debt_envs if (d.min_payment or 0) <= 0 and (d.target_amount or 0) - d.current_amount > 0]
        if active_personal:
            personal_lines = []
            for e in active_personal:
                rem = (e.target_amount or 0) - e.current_amount
                pct = int((e.current_amount / e.target_amount * 100)) if e.target_amount else 0
                pct_str = f", погашено <b>{pct}%</b>" if pct >= 10 else ""
                credit_lines_term = f" ({pct_str.lstrip(', ')})" if pct_str else ""
                paid_this_month = monthly_payments.get(e.id, 0.0) if monthly_payments else 0.0
                status = get_envelope_due_status_str(e, paid_this_month, current_day)
                status_suffix = f" — <i>{status}</i>" if status else ""
                due_str = f" (до {e.due_day}-го)" if e.due_day else ""
                personal_lines.append(f"• {e.name}{due_str}: осталось <b>{fmt_money(rem)}</b>{credit_lines_term}{status_suffix}")
            parts.append("\n🤝 <b>Долги близким:</b>\n" + "\n".join(personal_lines))
            
        all_active_debts = [d for d in debt_envs if (d.target_amount or 0) - d.current_amount > 0]
        if not all_active_debts:
            parts.append("🎉 <b>Поздравляем! У вас нет активных долгов.</b>")
            
        if monthly_income > 0:
            forecast_section = "\n🔮 <b>Прогноз до конца месяца:</b>\n"
            forecast = calculate_forecasts(envelopes, monthly_income)
            if forecast["status"] == "ok":
                free_cash = forecast["free_cash"]
                total_debt_reduction = 0
                if forecast.get("end_of_first_month_debts"):
                    for d in forecast["end_of_first_month_debts"]:
                        total_debt_reduction += (d["initial"] - d["remaining"])
                
                total_debt_start = sum((d.target_amount or 0) - d.current_amount for d in debt_envs)
                total_debt_end = max(0.0, total_debt_start - total_debt_reduction)
                
                max_months = 0
                if forecast.get("completed"):
                    for d_name, months in forecast["completed"].items():
                        target_env = next((env for env in envelopes if env.name == d_name), None)
                        if target_env and getattr(target_env, 'is_debt', False):
                            if months > max_months:
                                max_months = months

                forecast_lines = []
                forecast_lines.append(f"• 🟢 <b>Хватит на всё?</b> Да, останется <b>+{fmt_money(free_cash)}</b>")
                
                if total_debt_start > 0:
                    forecast_lines.append(f"• 📉 <b>Долги к след. месяцу:</b> <b>{fmt_money(total_debt_end)}</b> (снизятся на <b>{fmt_money(total_debt_reduction)}</b>)")
                    if max_months > 0:
                        forecast_lines.append(f"• ⏳ <b>Свобода от долгов:</b> {fmt_months_ru(max_months)}")
                    else:
                        forecast_lines.append(f"• ⏳ <b>Свобода от долгов:</b> долгов нет 🎉")
                else:
                    forecast_lines.append(f"• ⏳ <b>Свобода от долгов:</b> долгов нет 🎉")
                    
                forecast_section += "\n".join(forecast_lines)
                
            elif forecast["status"] == "negative_or_zero":
                free_cash = forecast["free_cash"]
                deficit_amt = -free_cash
                forecast_lines = [
                    f"• 🔴 <b>Хватит на всё?</b> Нет, дефицит <b>-{fmt_money(deficit_amt)}</b>",
                    "• ⚠️ <b>Совет:</b> сократите необязательные расходы или временно не вносите досрочные платежи.",
                    "• ⏳ <b>Свобода от долгов:</b> на паузе (требуется балансировка)"
                ]
                forecast_section += "\n".join(forecast_lines)
            parts.append(forecast_section)
        else:
            parts.append("\n💡 <i>Задайте планируемый доход (например: «мой доход 120к»), чтобы построить прогноз.</i>")

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
                
                text_clean = text.lower().strip()
                tab = 'navigator'
                if "расходы" in text_clean:
                    tab = 'expenses'
                elif "долги" in text_clean:
                    tab = 'debts'
                
                envelope_ids = [e.id for e in envelopes]
                monthly_payments = await get_monthly_payments(session, envelope_ids)
                monthly_spending = await get_monthly_spending(session, envelope_ids)
                
                dashboard = build_dashboard(
                    envelopes, 
                    monthly_income=budget_owner.monthly_income or 0,
                    tab=tab,
                    monthly_payments=monthly_payments,
                    monthly_spending=monthly_spending
                )
                
                session.add(ChatMessage(user_id=user.telegram_id, role="assistant", content=dashboard))
                await session.commit()
                
                reply_markup = None
                unallocated = _find_unallocated(envelopes)
                unallocated_amount = unallocated.current_amount if unallocated else 0.0
                if tab == 'navigator' and unallocated_amount > 0:
                    reply_markup = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text=f"📥 Распределить {fmt_money(unallocated_amount)}", callback_data="start_allocation")]
                    ])
                elif tab == 'expenses':
                    expense_envs = [
                        e for e in envelopes 
                        if not getattr(e, 'is_debt', False) 
                        and not getattr(e, 'is_goal', False) 
                        and "буфер" not in e.name.lower()
                        and e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")
                    ]
                    if expense_envs:
                        groups = {
                            "🏠 Жилье": [],
                            "🍔 Еда": [],
                            "🚗 Транспорт": [],
                            "❤️ Личное": [],
                            "📦 Прочее": []
                        }
                        for e in expense_envs:
                            grp = get_envelope_group(e.name)
                            groups[grp].append(e)
                            
                        keyboard_buttons = []
                        row = []
                        for grp_name, envs in groups.items():
                            if envs:
                                row.append(InlineKeyboardButton(text=f"🔍 {grp_name}", callback_data=f"detail_grp:{grp_name}"))
                                if len(row) == 2:
                                    keyboard_buttons.append(row)
                                    row = []
                        if row:
                            keyboard_buttons.append(row)
                        reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
                
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

            envelope_ids = [e.id for e in envelopes]
            monthly_payments = await get_monthly_payments(session, envelope_ids)
            env_context = build_envelopes_context(envelopes, monthly_payments)
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
            
            if not negotiation_context:
                is_allocate_request = (
                    text == "распредели свободные деньги"
                    or ("распредели" in text.lower() and ("свободн" in text.lower() or "кэш" in text.lower()))
                )
                if is_allocate_request:
                    unallocated = _find_unallocated(envelopes)
                    unallocated_amount = unallocated.current_amount if unallocated else 0.0
                    if unallocated_amount > 0:
                        negotiation_context = (
                            f"\n\n[SYSTEM: Пользователь просит распределить уже имеющиеся свободные деньги ({fmt_money(unallocated_amount)}). "
                            f"ОБЯЗАТЕЛЬНО: "
                            f"1. Выбери intent = 'transaction' (НЕ profile_update, так как бюджет уже настроен). "
                            f"2. Предложи распределить всю сумму ({fmt_money(unallocated_amount)}) в массиве income_allocations под ноль. "
                            f"3. В coach_reply опиши подробное предложение распределения (например: 'Предлагаю распределить {fmt_money(unallocated_amount)}:\n• 10к → Долг Сбер\n• 1к → Буфер\n\nПодтверди или предложи своё.').]"
                        )

            brain_response = await process_user_message(
                user_text=text + negotiation_context,
                user_vibe=user.prompt_vibe,
                envelopes_context=env_context,
                financial_health=financial_health,
                chat_history=chat_history_api
            )
            logger.info(f"LLM Response: {brain_response.model_dump_json() if hasattr(brain_response, 'model_dump_json') else brain_response}")

            existing_real_envs = [
                e for e in envelopes
                if e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")
            ]
            is_first_setup = len(existing_real_envs) == 0



            session.add(ChatMessage(user_id=user.telegram_id, role="user", content=text))

            extra_reply_parts = []
            if getattr(brain_response, 'envelopes_to_mark_paid', None):
                current_month_str = datetime.utcnow().strftime("%Y-%m")
                for name in brain_response.envelopes_to_mark_paid:
                    env = _find_envelope(envelopes, name)
                    if env:
                        env.last_paid_month = current_month_str
                        extra_reply_parts.append(
                            f"✅ Отметил статью <b>«{env.name}»</b> как оплаченную за этот месяц."
                        )
            show_envelopes_button = False

            is_already_confirming = False
            if state:
                current_state_check = await state.get_state()
                is_already_confirming = (current_state_check == IncomeStates.confirming.state)

            if brain_response.intent == "transaction" and brain_response.transactions:
                if state and is_already_confirming:
                    await state.clear()
                    is_already_confirming = False

                total_income_this_turn = 0.0
                unallocated_env_id = None

                for tx_data in brain_response.transactions:
                    if tx_data.action == "expense":
                        target_env = None
                        if tx_data.target_envelope_name:
                            target_env = _find_envelope(envelopes, tx_data.target_envelope_name)
                        
                        if not target_env and tx_data.target_envelope_name:
                            raw_name = tx_data.target_envelope_name
                            normalized_raw = _normalize_name(raw_name)
                            canonical_name = CANONICAL_ENVELOPE_MAPPING.get(normalized_raw)
                            
                            envelope_name = canonical_name if canonical_name else raw_name
                            target_env = _find_envelope(envelopes, envelope_name)
                            
                            if not target_env:
                                is_debt = "долг" in envelope_name.lower() or "кредит" in envelope_name.lower()
                                is_goal = any(w in envelope_name.lower() for w in ["отпуск", "подушка", "накоп", "на ", "цель"])
                                target_env = Envelope(
                                    user_id=budget_owner.telegram_id,
                                    name=envelope_name,
                                    current_amount=0.0,
                                    target_amount=0.0,
                                    is_debt=is_debt,
                                    is_goal=is_goal
                                )
                                session.add(target_env)
                                await session.flush()
                                envelopes.append(target_env)
                                
                                if not is_debt and not is_goal:
                                    extra_reply_parts.append(
                                        f"🛍 Создал статью <b>«{envelope_name}»</b> с лимитом 0. "
                                        f"Если хочешь установить лимит, просто напиши: <i>«лимит на {envelope_name.lower()} 10к»</i>"
                                    )
                                    
                        if not target_env:
                            target_env = _find_unallocated(envelopes)
                            if not target_env:
                                target_env = Envelope(
                                    user_id=budget_owner.telegram_id, name="Нераспределённые", current_amount=0.0
                                )
                                session.add(target_env)
                                await session.flush()
                                envelopes.append(target_env)

                        expense_amount = abs(tx_data.amount)
                        
                        if getattr(target_env, 'is_debt', False):
                            old_current = target_env.current_amount
                            target_env.current_amount += expense_amount
                            if target_env.target_amount and target_env.current_amount > target_env.target_amount:
                                target_env.current_amount = target_env.target_amount
                                
                            actual_paid = target_env.current_amount - old_current
                            
                            unallocated = _find_unallocated(envelopes)
                            if unallocated:
                                unallocated.current_amount -= actual_paid
                                # Double-entry: write negative transaction on unallocated
                                tx_unallocated = Transaction(
                                    user_id=user.telegram_id,
                                    amount=-actual_paid,
                                    envelope_id=unallocated.id,
                                    description=f"Списание на долг: {target_env.name}"
                                )
                                session.add(tx_unallocated)
                                
                            tx = Transaction(
                                user_id=user.telegram_id,
                                amount=actual_paid,
                                envelope_id=target_env.id,
                                description=tx_data.category or f"Оплата долга: {target_env.name}"
                            )
                            session.add(tx)
                            
                        else:
                            new_balance = target_env.current_amount - expense_amount

                            if new_balance < 0:
                                deficit = abs(new_balance)
                                unallocated = _find_unallocated(envelopes)
                                if unallocated and unallocated.current_amount > 0:
                                    transfer = min(unallocated.current_amount, deficit)
                                    unallocated.current_amount -= transfer
                                    target_env.current_amount += transfer
                                    
                                    # Double-entry: write transfer transactions
                                    tx_transfer_from = Transaction(
                                        user_id=user.telegram_id,
                                        amount=-transfer,
                                        envelope_id=unallocated.id,
                                        description=f"Перенос покрытия овердрафта: {target_env.name}"
                                    )
                                    tx_transfer_to = Transaction(
                                        user_id=user.telegram_id,
                                        amount=transfer,
                                        envelope_id=target_env.id,
                                        description=f"Покрытие овердрафта из Нераспределённых"
                                    )
                                    session.add(tx_transfer_from)
                                    session.add(tx_transfer_to)
                                    
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
                        if is_already_confirming:
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
                        
                        total_income_this_turn += actual_amount
                        unallocated_env_id = unallocated.id

                # After processing all transactions, set up the confirming state if there was any income
                if total_income_this_turn > 0:
                    if total_income_this_turn < 3000:
                        # Clear allocations to prevent small income confirmation flows
                        brain_response.income_allocations = None
                        brain_response.plan_items = None
                    else:
                        allocs = brain_response.income_allocations or brain_response.plan_items
                        if allocs and state:
                            alloc_names = [a.envelope_name if hasattr(a, 'envelope_name') else a.name for a in allocs]
                            alloc_amounts = [a.amount for a in allocs]
                            await state.set_state(IncomeStates.confirming)
                            await state.set_data({
                                "income_amount": total_income_this_turn,
                                "unallocated_env_id": unallocated_env_id,
                                "alloc_names": alloc_names,
                                "alloc_amounts": alloc_amounts
                            })

            elif brain_response.intent == "profile_update" and brain_response.envelopes_to_create:
                existing_real_envs = [
                    e for e in envelopes
                    if e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")
                ]
                is_first_setup = len(existing_real_envs) == 0

                changes = []
                if not is_first_setup:
                    # Update metadata of existing envelopes and create new ones.
                    # Never overwrite current_amount of existing envelopes to protect balance history.
                    affected_envs = []
                    for env_data in brain_response.envelopes_to_create:
                        existing = _find_envelope(envelopes, env_data.name)
                        if existing:
                            old_target = existing.target_amount
                            old_due = existing.due_day
                            old_min = existing.min_payment
                            
                            target_changed = (old_target != env_data.target_amount)
                            due_changed = (old_due != env_data.due_day)
                            min_changed = (old_min != env_data.min_payment)
                            
                            existing.target_amount = env_data.target_amount
                            existing.is_debt = env_data.is_debt
                            existing.is_goal = env_data.is_goal
                            existing.min_payment = env_data.min_payment
                            existing.due_day = env_data.due_day
                            affected_envs.append(existing)
                            
                            if target_changed or due_changed or min_changed:
                                change_parts = []
                                if target_changed:
                                    change_parts.append(f"лимит {fmt_money(old_target or 0)} → {fmt_money(env_data.target_amount or 0)}")
                                if due_changed:
                                    change_parts.append(f"срок до {old_due or '—'}-го → до {env_data.due_day or '—'}-го")
                                if min_changed:
                                    change_parts.append(f"мин. платеж {fmt_money(old_min or 0)} → {fmt_money(env_data.min_payment or 0)}")
                                changes.append(f"• <b>{existing.name}</b>: " + ", ".join(change_parts))
                        else:
                            new_env = Envelope(
                                user_id=budget_owner.telegram_id,
                                name=env_data.name,
                                target_amount=env_data.target_amount,
                                current_amount=0.0,
                                is_debt=env_data.is_debt,
                                is_goal=env_data.is_goal,
                                min_payment=env_data.min_payment,
                                due_day=env_data.due_day
                            )
                            session.add(new_env)
                            affected_envs.append(new_env)
                            changes.append(f"• <b>{new_env.name}</b> [Новый]: лимит {fmt_money(new_env.target_amount or 0)}")
                    await session.flush()
                    for ae in affected_envs:
                        if ae not in envelopes:
                            envelopes.append(ae)
                else:
                    # First-time setup — allow full creation (forced current_amount = 0.0)
                    affected_envs = []
                    for env_data in brain_response.envelopes_to_create:
                        existing = _find_envelope(envelopes, env_data.name)
                        if existing:
                            existing.target_amount = env_data.target_amount
                            existing.current_amount = 0.0
                            existing.is_debt = env_data.is_debt
                            existing.is_goal = env_data.is_goal
                            existing.min_payment = env_data.min_payment
                            existing.due_day = env_data.due_day
                            affected_envs.append(existing)
                        else:
                            new_env = Envelope(
                                user_id=budget_owner.telegram_id,
                                name=env_data.name,
                                target_amount=env_data.target_amount,
                                current_amount=0.0,
                                is_debt=env_data.is_debt,
                                is_goal=env_data.is_goal,
                                min_payment=env_data.min_payment,
                                due_day=env_data.due_day
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
                        user_id=budget_owner.telegram_id, name="Нераспределённые", current_amount=0.0
                    )
                    session.add(unallocated)
                    await session.flush()
                    envelopes.append(unallocated)
                
                # Идемпотентность: перезаписываем кэш, а не прибавляем!
                if brain_response.free_cash is not None:
                    if is_first_setup:
                        await session.execute(delete(Transaction).where(Transaction.user_id == budget_owner.telegram_id))
                        await session.flush()
                        unallocated.current_amount = brain_response.free_cash
                        if brain_response.free_cash > 0:
                            tx = Transaction(
                                user_id=budget_owner.telegram_id,
                                amount=brain_response.free_cash,
                                envelope_id=unallocated.id,
                                description="Стартовый капитал"
                            )
                            session.add(tx)
                    else:
                        diff = brain_response.free_cash - unallocated.current_amount
                        if abs(diff) > 0.01:
                            unallocated.current_amount = brain_response.free_cash
                            tx = Transaction(
                                user_id=budget_owner.telegram_id,
                                amount=diff,
                                envelope_id=unallocated.id,
                                description="Корректировка баланса при обновлении профиля"
                            )
                            session.add(tx)

                if getattr(brain_response, 'plan_items', None) and state:
                    allocs = brain_response.plan_items
                    alloc_names = [a.envelope_name if hasattr(a, 'envelope_name') else a.name for a in allocs]
                    alloc_amounts = [a.amount for a in allocs]
                    
                    # Store in FSMContext
                    await state.set_state(IncomeStates.confirming)
                    await state.set_data({
                        "income_amount": brain_response.free_cash or 0.0,
                        "unallocated_env_id": unallocated.id,
                        "alloc_names": alloc_names,
                        "alloc_amounts": alloc_amounts
                    })
                            
                env_count = len([e for e in envelopes if e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")])
                if is_first_setup:
                    brain_response.coach_reply += f"\n\n📊 Бюджет сформирован ({env_count} категорий)"
                else:
                    if changes:
                        brain_response.coach_reply = "✅ <b>Обновил бюджет:</b>\n" + "\n".join(changes)
                    else:
                        brain_response.coach_reply += f"\n\n📊 Бюджет обновлен ({env_count} категорий)"

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
                
                envelope_ids2 = [e.id for e in fresh_envelopes]
                monthly_payments2 = await get_monthly_payments(session, envelope_ids2)
                monthly_spending2 = await get_monthly_spending(session, envelope_ids2)
                
                brain_response.coach_reply += "\n\n" + build_dashboard(
                    fresh_envelopes, 
                    monthly_income=budget_owner.monthly_income or 0, 
                    tab='navigator',
                    monthly_payments=monthly_payments2,
                    monthly_spending=monthly_spending2
                )
            elif brain_response.intent == "transaction" and brain_response.transactions:
                await session.flush()
                env_result2 = await session.execute(select(Envelope).where(Envelope.user_id == budget_owner.telegram_id))
                fresh_envelopes = list(env_result2.scalars().all())
                
                envelope_ids = [e.id for e in fresh_envelopes]
                monthly_payments = await get_monthly_payments(session, envelope_ids)
                
                pending_alloc_amt = 0.0
                if brain_response.income_allocations:
                    pending_alloc_amt = sum(a.amount for a in brain_response.income_allocations)
                
                micro_nav = build_micro_navigator(
                    fresh_envelopes, 
                    brain_response.transactions, 
                    monthly_payments,
                    pending_allocation_amount=pending_alloc_amt
                )
                brain_response.coach_reply += "\n" + micro_nav

            session.add(ChatMessage(user_id=user.telegram_id, role="assistant", content=brain_response.coach_reply))
            if not await verify_user_ledger(session, budget_owner.telegram_id):
                await session.rollback()
                raise ValueError("Ledger invariant violated!")
            await session.commit()

        safe_reply = brain_response.coach_reply
        for old, new in [("<br>", "\n"), ("<br/>", "\n"), ("</br>", ""), ("<p>", ""), ("</p>", "\n"),
                         ("<strong>", "<b>"), ("</strong>", "</b>"), ("<em>", "<i>"), ("</em>", "</i>"),
                         ("<ul>", ""), ("</ul>", ""), ("<ol>", ""), ("</ol>", ""),
                         ("<li>", "• "), ("</li>", "\n")]:
            safe_reply = safe_reply.replace(old, new)
        safe_reply = safe_reply.replace("Дашборд", "Навигатор").replace("дашборд", "навигатор")

        is_profile_update = brain_response.intent == "profile_update"
        allocs = brain_response.income_allocations or (brain_response.plan_items if not is_profile_update else None)
        if allocs and state:
            current_state = await state.get_state()
            alloc_names = [a.envelope_name if hasattr(a, 'envelope_name') else a.name for a in allocs]
            alloc_amounts = [a.amount for a in allocs]
            
            if current_state != IncomeStates.confirming.state:
                # Check if this is allocating existing unallocated cash vs new income auto-inference
                is_existing_allocation = (text == "распредели свободные деньги")
                total_income = sum(a.amount for a in allocs)
                unallocated = _find_unallocated(envelopes)
                if unallocated:
                    if not is_existing_allocation:
                        unallocated.current_amount += total_income
                        tx = Transaction(
                            user_id=user.telegram_id,
                            amount=total_income,
                            envelope_id=unallocated.id,
                            description="Доход (авто-возобновление)"
                        )
                        session.add(tx)
                        if not await verify_user_ledger(session, budget_owner.telegram_id):
                            await session.rollback()
                            raise ValueError("Ledger invariant violated!")
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
        is_pending_paid = False
        
        # Consolidate confirmation sources
        confirm_names = []
        if getattr(brain_response, 'pending_paid_confirmations', None):
            confirm_names.extend(brain_response.pending_paid_confirmations)
        elif getattr(brain_response, 'pending_paid_confirmation', None):
            confirm_names.append(brain_response.pending_paid_confirmation)
            
        if confirm_names and state:
            valid_envs = []
            for name in confirm_names:
                env = _find_envelope(envelopes, name)
                if env:
                    valid_envs.append(env)
            
            if valid_envs:
                await state.set_state(IncomeStates.confirming_paid_limit)
                await state.set_data({
                    "envelope_names": [env.name for env in valid_envs]
                })
                
                if len(valid_envs) == 1:
                    env = valid_envs[0]
                    limit = env.min_payment if env.is_debt else env.target_amount
                    limit_val = limit or 0.0
                    safe_reply = f"Отметить статью <b>«{env.name}»</b> полностью оплаченной в этом месяце ({fmt_money(limit_val)})?"
                else:
                    lines = []
                    for env in valid_envs:
                        limit = env.min_payment if env.is_debt else env.target_amount
                        limit_val = limit or 0.0
                        lines.append(f"• <b>{env.name}</b> ({fmt_money(limit_val)})")
                    safe_reply = "Отметить эти статьи полностью оплаченными в этом месяце?\n" + "\n".join(lines)
                
                reply_markup = InlineKeyboardMarkup(inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✅ Да", callback_data="confirm_paid_yes"),
                        InlineKeyboardButton(text="✏️ Другая сумма", callback_data="confirm_paid_no")
                    ]
                ])
                is_pending_paid = True

        if not is_pending_paid:
            is_profile_update = brain_response.intent == "profile_update"
            has_allocs = False
            if is_profile_update:
                has_allocs = bool(brain_response.plan_items and brain_response.free_cash and brain_response.free_cash > 0)
            else:
                has_allocs = bool(brain_response.income_allocations or brain_response.plan_items)
                
            if has_allocs:
                reply_markup = InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✅ Да, применить", callback_data="confirm_income")],
                    [InlineKeyboardButton(text="❌ Оставить в нераспределенных", callback_data="reject_income")]
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

    except Exception as e:
        logger.error(f"Error processing transaction: {e}", exc_info=True)
        await message.answer("Блин, не совсем понял. Можешь повторить подробнее?")

    finally:
        # Clean up loading messages AFTER sending final answer or on error
        if 'animation_task' in locals() and animation_task:
            animation_task.cancel()
            
        if 'loading_msgs' in locals() and loading_msgs:
            for msg in loading_msgs:
                try:
                    await msg.delete()
                except Exception:
                    pass


@router.callback_query(F.data == "confirm_income", IncomeStates.confirming)
async def confirm_income(callback, state: FSMContext):
    data = await state.get_data()
    income_amount = data.get("income_amount", 0)
    unallocated_env_id = data.get("unallocated_env_id")
    alloc_names = data.get("alloc_names", [])
    alloc_amounts = data.get("alloc_amounts", [])

    try:
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
                transfer_amount = min(amount, max(0.0, unallocated.current_amount))
                if getattr(target_env, 'is_debt', False) and target_env.target_amount:
                    remaining_debt = target_env.target_amount - target_env.current_amount
                    if transfer_amount > remaining_debt:
                        transfer_amount = max(0.0, remaining_debt)
                
                if transfer_amount <= 0:
                    continue

                unallocated.current_amount -= transfer_amount
                target_env.current_amount += transfer_amount

                # Double-entry transactions
                tx_from = Transaction(
                    user_id=callback.from_user.id,
                    amount=-transfer_amount,
                    envelope_id=unallocated.id,
                    description=f"Распределение: {target_env.name}"
                )
                tx_to = Transaction(
                    user_id=callback.from_user.id,
                    amount=transfer_amount,
                    envelope_id=target_env.id,
                    description=f"Распределение дохода: {name}"
                )
                session.add(tx_from)
                session.add(tx_to)
                distribution_text_parts.append(f"• {fmt_money(transfer_amount)} → {name}")

            if not await verify_user_ledger(session, budget_owner.telegram_id):
                await session.rollback()
                logger.error(f"Ledger verification failed in confirm_income for user {budget_owner.telegram_id}")
                await callback.message.answer("⚠️ Ошибка проверки баланса. Пожалуйста, обратитесь в поддержку.")
                await state.clear()
                return

            await session.commit()

        reply = f"✅ <b>Распределил {fmt_money(income_amount)}:</b>\n" + "\n".join(distribution_text_parts)
        await callback.message.edit_text(reply, parse_mode="HTML")
        await state.clear()

    except Exception as e:
        logger.error(f"Error in confirm_income: {e}", exc_info=True)
        await callback.message.answer("Что-то пошло не так при распределении. Попробуй ещё раз.")
        await state.clear()


@router.callback_query(F.data == "reject_income", IncomeStates.confirming)
async def reject_income(callback, state: FSMContext):
    await callback.message.edit_text(
        "👌 Деньги остались в <b>Нераспределённых</b>. Скажи, когда решишь, куда их направить.",
        parse_mode="HTML"
    )
    await state.clear()


@router.callback_query(F.data == "confirm_paid_yes", IncomeStates.confirming_paid_limit)
async def confirm_paid_yes(callback: CallbackQuery, state: FSMContext):
    state_data = await state.get_data()
    env_names = state_data.get("envelope_names")
    if not env_names and state_data.get("envelope_name"):
        env_names = [state_data["envelope_name"]]
        
    current_month_str = datetime.utcnow().strftime("%Y-%m")
    
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = user_result.scalar_one_or_none()
        
        budget_owner = user
        if user and user.family_host_id:
            host_result = await session.execute(select(User).where(User.telegram_id == user.family_host_id))
            host = host_result.scalar_one_or_none()
            if host:
                budget_owner = host

        marked_names = []
        if env_names:
            env_result = await session.execute(
                select(Envelope).where(Envelope.user_id == budget_owner.telegram_id)
            )
            all_envelopes = list(env_result.scalars().all())
            
            for name in env_names:
                env = _find_envelope(all_envelopes, name)
                if env:
                    env.last_paid_month = current_month_str
                    marked_names.append(env.name)
                    
        if marked_names:
            await session.commit()
            if len(marked_names) == 1:
                await callback.message.edit_text(
                    f"✅ Отметил статью <b>«{marked_names[0]}»</b> как оплаченную за этот месяц.",
                    parse_mode="HTML"
                )
            else:
                formatted_list = "\n".join(f"• <b>«{name}»</b>" for name in marked_names)
                await callback.message.edit_text(
                    f"✅ Отметил статьи как оплаченные за этот месяц:\n{formatted_list}",
                    parse_mode="HTML"
                )
        else:
            await callback.message.edit_text("Не удалось найти статьи расходов.")
            
    await state.clear()


@router.callback_query(F.data == "confirm_paid_no", IncomeStates.confirming_paid_limit)
async def confirm_paid_no(callback: CallbackQuery, state: FSMContext):
    state_data = await state.get_data()
    env_names = state_data.get("envelope_names")
    if not env_names and state_data.get("envelope_name"):
        env_names = [state_data["envelope_name"]]
        
    if env_names and len(env_names) == 1:
        await callback.message.edit_text(
            f"Окей! Тогда напиши точную сумму оплаты, например: <i>«оплатил {env_names[0]} 900»</i>",
            parse_mode="HTML"
        )
    else:
        await callback.message.edit_text(
            "Окей! Тогда напиши точные суммы текстом, например: <i>«аренда 30000, интернет 900»</i>",
            parse_mode="HTML"
        )
    await state.clear()


@router.callback_query(F.data.startswith("detail_grp:"))
async def show_group_details(callback: CallbackQuery):
    grp_name = callback.data.split(":", 1)[1]
    
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
                
        env_result = await session.execute(select(Envelope).where(Envelope.user_id == budget_owner.telegram_id))
        envelopes = list(env_result.scalars().all())
        
        expense_envs = [
            e for e in envelopes 
            if not getattr(e, 'is_debt', False) 
            and not getattr(e, 'is_goal', False) 
            and "буфер" not in e.name.lower()
            and e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")
        ]
        
        grp_envs = [e for e in expense_envs if get_envelope_group(e.name) == grp_name]
        
        if not grp_envs:
            await callback.answer("Нет трат в этой категории", show_alert=True)
            return
            
        await callback.answer()
        
        envelope_ids = [e.id for e in grp_envs]
        monthly_spending = await get_monthly_spending(session, envelope_ids)
        current_day = datetime.utcnow().day
        
        lines = []
        for e in grp_envs:
            limit_str = f" из <b>{fmt_money(e.target_amount or 0)}</b>" if e.target_amount else ""
            spent = monthly_spending.get(e.id, 0.0)
            status = get_envelope_due_status_str(e, spent, current_day)
            status_suffix = f" — <i>{status}</i>" if status else ""
            due_str = f" (до {e.due_day}-го)" if e.due_day else ""
            lines.append(f"• {e.name}{due_str}: доступно <b>{fmt_money(e.current_amount)}</b>{limit_str}{status_suffix}")
            
        text = (
            f"🔍 <b>Детализация категории: {grp_name}</b>\n\n"
            + "\n".join(lines)
        )
        
        reply_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад к категориям", callback_data="back_to_expenses")]
        ])
        
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)


@router.callback_query(F.data == "back_to_expenses")
async def back_to_expenses_callback(callback: CallbackQuery):
    await callback.answer()
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
                
        env_result = await session.execute(select(Envelope).where(Envelope.user_id == budget_owner.telegram_id))
        envelopes = list(env_result.scalars().all())
        
        envelope_ids = [e.id for e in envelopes]
        monthly_payments = await get_monthly_payments(session, envelope_ids)
        monthly_spending = await get_monthly_spending(session, envelope_ids)
        
        dashboard = build_dashboard(
            envelopes, 
            monthly_income=budget_owner.monthly_income or 0,
            tab='expenses',
            monthly_payments=monthly_payments,
            monthly_spending=monthly_spending
        )
        
        expense_envs = [
            e for e in envelopes 
            if not getattr(e, 'is_debt', False) 
            and not getattr(e, 'is_goal', False) 
            and "буфер" not in e.name.lower()
            and e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")
        ]
        
        groups = {
            "🏠 Жилье": [],
            "🍔 Еда": [],
            "🚗 Транспорт": [],
            "❤️ Личное": [],
            "📦 Прочее": []
        }
        for e in expense_envs:
            grp = get_envelope_group(e.name)
            groups[grp].append(e)
            
        keyboard_buttons = []
        row = []
        for grp_name, envs in groups.items():
            if envs:
                row.append(InlineKeyboardButton(text=f"🔍 {grp_name}", callback_data=f"detail_grp:{grp_name}"))
                if len(row) == 2:
                    keyboard_buttons.append(row)
                    row = []
        if row:
            keyboard_buttons.append(row)
            
        reply_markup = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
        
        await callback.message.edit_text(dashboard, parse_mode="HTML", reply_markup=reply_markup)


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
        
        envelope_ids = [e.id for e in envelopes]
        monthly_payments = await get_monthly_payments(session, envelope_ids)
        monthly_spending = await get_monthly_spending(session, envelope_ids)

    if not envelopes:
        await callback.answer("Бюджет пока пуст")
        return

    dashboard = build_dashboard(
        envelopes, 
        monthly_income=monthly_income, 
        tab='navigator', 
        monthly_payments=monthly_payments,
        monthly_spending=monthly_spending
    )
    
    reply_markup = None
    unallocated = _find_unallocated(envelopes)
    unallocated_amount = unallocated.current_amount if unallocated else 0.0
    if unallocated_amount > 0:
        reply_markup = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"📥 Распределить {fmt_money(unallocated_amount)}", callback_data="start_allocation")]
        ])
        
    try:
        await callback.message.edit_text(dashboard, parse_mode="HTML", reply_markup=reply_markup)
    except Exception:
        await callback.message.answer(dashboard, parse_mode="HTML", reply_markup=reply_markup)


from aiogram.filters import Command

@router.message(Command("allocate"))
async def cmd_allocate(message: Message, state: FSMContext):
    await message.chat.do("typing")
    await handle_transaction(message, "распредели свободные деньги", state=state)


@router.callback_query(F.data == "start_allocation")
async def start_allocation_callback(callback, state: FSMContext):
    await callback.answer()
    msg = callback.message.model_copy(update={"from_user": callback.from_user})
    await handle_transaction(msg, "распредели свободные деньги", state=state)


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
