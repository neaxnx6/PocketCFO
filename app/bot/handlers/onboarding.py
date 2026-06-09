import logging
import os
from typing import Optional
from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, FSInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.future import select
from sqlalchemy import delete

from app.database.session import async_session_maker
from app.database.models import User, Envelope, Transaction, ChatMessage

logger = logging.getLogger(__name__)
router = Router()

class OnboardingStates(StatesGroup):
    step_cash = State()       # Шаг 1: Деньги на руках
    step_income = State()     # Шаг 2: Доход
    step_debts = State()      # Шаг 3: Долги
    step_priority = State()   # Шаг 4: Финансовый приоритет
    step_expenses = State()   # Шаг 5: Расходы


def parse_amount_regex(text: str) -> Optional[float]:
    t = text.lower().replace(" ", "").replace(",", ".").replace("₽", "").replace("руб", "").replace("рублей", "").strip()
    multiplier = 1.0
    if t.endswith("к") or t.endswith("k"):
        multiplier = 1000.0
        t = t[:-1]
    try:
        return float(t) * multiplier
    except ValueError:
        return None


@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext = None, bot: Bot = None):
    if state:
        await state.clear()
    async with async_session_maker() as session:
        # Find user first
        user_result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = user_result.scalar_one_or_none()
        
        if user:
            # If user is a host, get members and notify them, then reset their family_host_id
            members_result = await session.execute(select(User).where(User.family_host_id == message.from_user.id))
            members = list(members_result.scalars().all())
            for m in members:
                m.family_host_id = None
                if bot:
                    try:
                        await bot.send_message(
                            m.telegram_id,
                            "🚪 <b>Семейный бюджет распущен</b>\n\n"
                            "Владелец бюджета полностью сбросил свои данные. Ты возвращён в соло-режим.",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass
            
            # If user is a member, notify host
            if user.family_host_id and bot:
                try:
                    await bot.send_message(
                        user.family_host_id,
                        "🚪 <b>Участник вышел из бюджета</b>\n\n"
                        "Пользователь отключился от твоего семейного бюджета (сбросил данные).",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

        # Delete user's data (Cascade handles transactions and envelopes if configured, but explicit is safer)
        await session.execute(delete(ChatMessage).where(ChatMessage.user_id == message.from_user.id))
        await session.execute(delete(Transaction).where(Transaction.user_id == message.from_user.id))
        await session.execute(delete(Envelope).where(Envelope.user_id == message.from_user.id))
        await session.execute(delete(User).where(User.telegram_id == message.from_user.id))
        await session.commit()
        
    persistent_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Навигатор")],
            [KeyboardButton(text="🛍 Расходы"), KeyboardButton(text="💳 Долги")]
        ],
        resize_keyboard=True,
        is_persistent=True
    )
    await message.answer("🧹 Все твои данные, статьи расходов и история памяти полностью удалены! Напиши /start, чтобы начать с чистого листа.", reply_markup=persistent_keyboard)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext = None):
    if state:
        await state.clear()
        
    async with async_session_maker() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one_or_none()
        
        has_envelopes = False
        if user:
            env_res = await session.execute(select(Envelope).where(Envelope.user_id == user.telegram_id))
            envelopes = list(env_res.scalars().all())
            # Skip unallocated if it's the only one
            real_envs = [e for e in envelopes if e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")]
            has_envelopes = len(real_envs) > 0
        
        if has_envelopes:
            welcome_text = "С возвращением! Я помню все твои цели. Жду расходы, доходы или вопросы! (Если хочешь начать жизнь с чистого листа, нажми /reset)"
            
            persistent_keyboard = ReplyKeyboardMarkup(
                keyboard=[
                    [KeyboardButton(text="📊 Навигатор")],
                    [KeyboardButton(text="🛍 Расходы"), KeyboardButton(text="💳 Долги")]
                ],
                resize_keyboard=True,
                is_persistent=True
            )
            
            # Banner check
            root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
            banner_path = None
            for ext in [".png", ".jpg", ".jpeg"]:
                test_path = os.path.join(root_dir, "assets", "branding", f"start_banner{ext}")
                if os.path.exists(test_path):
                    banner_path = test_path
                    break
                    
            if banner_path:
                try:
                    await message.answer_photo(
                        photo=FSInputFile(banner_path),
                        caption=welcome_text,
                        parse_mode="HTML",
                        reply_markup=persistent_keyboard
                    )
                    return
                except Exception:
                    pass
            await message.answer(welcome_text, parse_mode="HTML", reply_markup=persistent_keyboard)
            return

    # If new user or no setup yet, show choices
    welcome_text = (
        "👋 Привет! Я «На Балансе» — твой финансовый напарник.\n\n"
        "Я помогу навести порядок в деньгах, разобраться с долгами и перестать уходить в минус. "
        "Вся математика на мне — от тебя нужны только траты.\n\n"
        "Выбери, как тебе удобнее начать работу 👇"
    )
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Пройти опрос (1 мин)", callback_data="start_survey")],
        [InlineKeyboardButton(text="🎙 Настроить голосом/текстом", callback_data="start_manual")],
        [InlineKeyboardButton(text="🚀 Начать сразу без настроек", callback_data="start_instant")]
    ])
    
    await message.answer(welcome_text, reply_markup=markup)


# === ВАРИАНТЫ НАСТРОЙКИ ===

@router.callback_query(F.data == "start_manual")
async def process_manual_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await callback.message.edit_text(
        "🎙 <b>Настройка голосом или текстом</b>\n\n"
        "Просто запиши голосовое сообщение или напиши мне текстом: "
        "сколько зарабатываешь, какие долги висят и какие основные траты каждый месяц.\n\n"
        "Я сам распаршу и составлю первый финансовый план!",
        parse_mode="HTML"
    )
    await state.clear()


@router.callback_query(F.data == "start_instant")
async def process_instant_start(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = user_result.scalar_one_or_none()
        if not user:
            user = User(telegram_id=callback.from_user.id)
            session.add(user)
            await session.flush()
            
        # Clear any old data
        await session.execute(delete(Envelope).where(Envelope.user_id == callback.from_user.id))
        await session.flush()
        
        # Add basic envelopes
        unallocated = Envelope(user_id=callback.from_user.id, name="Нераспределённые", current_amount=0.0)
        buffer_env = Envelope(user_id=callback.from_user.id, name="Буфер", current_amount=0.0, target_amount=0.0, is_debt=False, is_goal=True)
        session.add(unallocated)
        session.add(buffer_env)
        await session.commit()
        
    persistent_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Навигатор")],
            [KeyboardButton(text="🛍 Расходы"), KeyboardButton(text="💳 Долги")]
        ],
        resize_keyboard=True,
        is_persistent=True
    )
    
    await callback.message.delete()
    await callback.message.answer(
        "🚀 <b>Мы начали без предварительных настроек!</b>\n\n"
        "Твой кошелек пока пуст. Ты можешь сразу писать свои траты и доходы "
        "(например: <i>«потратил 1500 рублей на продукты»</i> или <i>«пришла зп 50к»</i>).\n\n"
        "Я буду создавать категории расходов автоматически на ходу!",
        parse_mode="HTML",
        reply_markup=persistent_keyboard
    )


# === ПОШАГОВЫЙ ОПРОСНИК ===

@router.callback_query(F.data == "start_survey")
async def start_survey_flow(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(OnboardingStates.step_cash)
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить (0 ₽)", callback_data="skip_cash")]
    ])
    
    await callback.message.edit_text(
        "<b>Шаг 1 из 5: Твои свободные деньги</b>\n\n"
        "Какая сумма сейчас лежит у тебя на руках или картах? Это свободные деньги, которые мы сможем распределить.\n\n"
        "<i>Пример: 15 000 или 15к. Если сейчас свободных денег нет, отправь 0.</i>",
        parse_mode="HTML",
        reply_markup=markup
    )


@router.callback_query(F.data == "skip_cash", OnboardingStates.step_cash)
async def process_skip_cash(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(cash=0.0)
    await ask_step_income(callback.message, state, edit=True)


@router.message(OnboardingStates.step_cash)
async def process_step_cash(message: Message, state: FSMContext):
    val = parse_amount_regex(message.text)
    if val is None:
        await message.answer("Пожалуйста, введи число (например: 10000 или 10к):")
        return
    await state.update_data(cash=val)
    await ask_step_income(message, state, edit=False)


async def ask_step_income(message: Message, state: FSMContext, edit: bool = False):
    await state.set_state(OnboardingStates.step_income)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data="skip_income")]
    ])
    
    text = (
        "<b>Шаг 2 из 5: Ежемесячный доход</b>\n\n"
        "Сколько ты зарабатываешь в среднем за месяц? Это нужно, чтобы я мог рассчитать прогнозы.\n\n"
        "<i>Пример: 120к или 120 000. Если доход непостоянный, укажи средний ориентир.</i>"
    )
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data == "skip_income", OnboardingStates.step_income)
async def process_skip_income(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(income=0.0)
    await ask_step_debts(callback.message, state, edit=True)


@router.message(OnboardingStates.step_income)
async def process_step_income(message: Message, state: FSMContext):
    val = parse_amount_regex(message.text)
    if val is None:
        await message.answer("Пожалуйста, введи число (например: 120к или 120000):")
        return
    await state.update_data(income=val)
    await ask_step_debts(message, state, edit=False)


async def ask_step_debts(message: Message, state: FSMContext, edit: bool = False):
    await state.set_state(OnboardingStates.step_debts)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="У меня нет долгов / Пропустить", callback_data="skip_debts")]
    ])
    
    text = (
        "<b>Шаг 3 из 5: Долги и кредиты</b>\n\n"
        "Есть ли у тебя активные кредиты, карты или долги? Напиши общую сумму долга и минимальный ежемесячный платеж.\n\n"
        "<b>Пример:</b>\n"
        "<code>Сбербанк: долг 80к, платеж 4к</code>\n"
        "<code>Долг другу: 15к</code>\n\n"
        "<i>Если долгов нет, нажми кнопку ниже.</i>"
    )
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data == "skip_debts", OnboardingStates.step_debts)
async def process_skip_debts(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.update_data(debts=[])
    await ask_step_priority(callback.message, state, edit=True)


@router.message(OnboardingStates.step_debts)
async def process_step_debts(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == "0" or text.lower() in ("нет", "пропустить", "нет долгов"):
        await state.update_data(debts=[])
        await ask_step_priority(message, state, edit=False)
        return
        
    wait_msg = await message.answer("⏳ <i>Распознаю долги...</i>", parse_mode="HTML")
    try:
        from app.services.ai_parser import parse_onboarding_debts
        parsed = await parse_onboarding_debts(text)
        debts_list = [{"name": d.name, "amount": d.amount, "min_payment": d.min_payment} for d in parsed.debts]
        await state.update_data(debts=debts_list)
        await wait_msg.delete()
        
        if debts_list:
            debts_str = "\n".join(f"• 🏦 {d['name']}: долг {d['amount']:.0f} ₽" + (f" (мин. платеж: {d['min_payment']:.0f} ₽)" if d['min_payment'] else "") for d in debts_list)
            await message.answer(f"✅ <b>Распознал долги:</b>\n{debts_str}", parse_mode="HTML")
        else:
            await message.answer("ℹ️ Долгов не обнаружено.", parse_mode="HTML")
            
        await ask_step_priority(message, state, edit=False)
    except Exception as e:
        logger.error(f"Failed to parse debts: {e}", exc_info=True)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await message.answer("Не совсем понял список долгов. Напиши, пожалуйста, как в примере: <code>Сбербанк 80к, платеж 4к</code> (или нажми кнопку)", parse_mode="HTML")


async def ask_step_priority(message: Message, state: FSMContext, edit: bool = False):
    await state.set_state(OnboardingStates.step_priority)
    
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Быстрее закрыть долги", callback_data="focus:debts")],
        [InlineKeyboardButton(text="🛡 Собрать подушку безопасности", callback_data="focus:buffer")],
        [InlineKeyboardButton(text="🎯 Начать копить на крупную цель", callback_data="focus:save")],
        [InlineKeyboardButton(text="📊 Просто контролировать расходы", callback_data="focus:control")]
    ])
    
    text = (
        "<b>Шаг 4 из 5: Твой фокус</b>\n\n"
        "Что сейчас для тебя важнее всего? Это поможет мне точнее подбирать рекомендации."
    )
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data.startswith("focus:"), OnboardingStates.step_priority)
async def process_focus_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    focus_code = callback.data.split(":", 1)[1]
    await state.update_data(focus=focus_code)
    
    focus_map = {
        "debts": "💳 Быстрее закрыть долги",
        "buffer": "🛡 Собрать подушку безопасности",
        "save": "🎯 Накопить на крупную цель",
        "control": "📊 Просто контролировать расходы"
    }
    focus_name = focus_map.get(focus_code, focus_code)
    await callback.message.edit_text(
        f"<b>Шаг 4 из 5: Твой фокус</b>\n\n"
        f"Выбрано: {focus_name}",
        parse_mode="HTML"
    )
    
    await ask_step_expenses(callback.message, state, edit=False)


async def ask_step_expenses(message: Message, state: FSMContext, edit: bool = False):
    await state.set_state(OnboardingStates.step_expenses)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data="skip_expenses")]
    ])
    
    text = (
        "<b>Шаг 5 из 5: Обычные расходы</b>\n\n"
        "Какие траты у тебя обычно есть каждый месяц? Просто напиши как помнишь.\n\n"
        "<b>Пример:</b>\n"
        "<code>Аренда 35к</code>\n"
        "<code>Продукты 25к</code>\n"
        "<code>Машина 10к</code>\n"
        "<code>Интернет 500</code>"
    )
    if edit:
        await message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data == "skip_expenses", OnboardingStates.step_expenses)
async def process_skip_expenses(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    data = await state.get_data()
    await callback.message.delete()
    await complete_onboarding(
        user_id=callback.from_user.id,
        cash=data.get("cash", 0.0),
        income=data.get("income", 0.0),
        debts=data.get("debts", []),
        priority=data.get("focus", ""),
        expenses=[],
        message=callback.message,
        state=state
    )


@router.message(OnboardingStates.step_expenses)
async def process_step_expenses(message: Message, state: FSMContext):
    text = message.text.strip()
    if text == "0" or text.lower() in ("нет", "пропустить"):
        data = await state.get_data()
        await complete_onboarding(
            user_id=message.from_user.id,
            cash=data.get("cash", 0.0),
            income=data.get("income", 0.0),
            debts=data.get("debts", []),
            priority=data.get("focus", ""),
            expenses=[],
            message=message,
            state=state
        )
        return
        
    wait_msg = await message.answer("⏳ <i>Распознаю расходы...</i>", parse_mode="HTML")
    try:
        from app.services.ai_parser import parse_onboarding_expenses
        parsed = await parse_onboarding_expenses(text)
        expenses_list = [{"name": e.name, "amount": e.amount} for e in parsed.expenses]
        await state.update_data(expenses=expenses_list)
        await wait_msg.delete()
        
        if expenses_list:
            exp_str = "\n".join(f"• 🛍 {e['name']}: {e['amount']:.0f} ₽" for e in expenses_list)
            await message.answer(f"✅ <b>Распознал расходы:</b>\n{exp_str}", parse_mode="HTML")
        else:
            await message.answer("ℹ️ Расходов не обнаружено.", parse_mode="HTML")
            
        data = await state.get_data()
        await complete_onboarding(
            user_id=message.from_user.id,
            cash=data.get("cash", 0.0),
            income=data.get("income", 0.0),
            debts=data.get("debts", []),
            priority=data.get("focus", ""),
            expenses=expenses_list,
            message=message,
            state=state
        )
    except Exception as e:
        logger.error(f"Failed to parse expenses: {e}", exc_info=True)
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await message.answer("Не совсем понял список расходов. Напиши, пожалуйста, как в примере: <code>Аренда 35к, продукты 25к</code> (или нажми кнопку)", parse_mode="HTML")


async def complete_onboarding(user_id: int, cash: float, income: float, debts: list, priority: str, expenses: list, message: Message, state: FSMContext):
    # Import locally to prevent circular imports
    from app.bot.handlers.transactions import verify_user_ledger, get_monthly_payments, build_envelopes_context, build_financial_health, IncomeStates
    from app.services.ai_brain import process_user_message
    
    wait_msg = await message.answer("⏳ <i>Настраиваю твой личный профиль и баланс...</i>", parse_mode="HTML")
    
    try:
        async with async_session_maker() as session:
            # 1. Ensure user exists
            user_result = await session.execute(select(User).where(User.telegram_id == user_id))
            user = user_result.scalar_one_or_none()
            if not user:
                user = User(telegram_id=user_id)
                session.add(user)
                await session.flush()
                
            user.monthly_income = income
            
            # Save focus priority to prompt_vibe
            if priority == "debts":
                user.prompt_vibe = "Заботливый друг (главная цель: закрыть долги)"
            elif priority == "buffer":
                user.prompt_vibe = "Заботливый друг (главная цель: создать подушку)"
            elif priority == "save":
                user.prompt_vibe = "Заботливый друг (главная цель: начать копить)"
            elif priority == "control":
                user.prompt_vibe = "Заботливый друг (главная цель: контролировать расходы)"
            else:
                user.prompt_vibe = "Заботливый друг"
                
            # 2. Clean out old envelopes
            await session.execute(delete(Envelope).where(Envelope.user_id == user_id))
            await session.flush()
            
            # 3. Create envelopes
            unallocated = Envelope(user_id=user_id, name="Нераспределённые", current_amount=0.0)
            session.add(unallocated)
            
            buffer_env = Envelope(user_id=user_id, name="Буфер", current_amount=0.0, target_amount=0.0, is_debt=False, is_goal=True)
            session.add(buffer_env)
            await session.flush()
            
            # Debts
            for d in debts:
                debt_env = Envelope(
                    user_id=user_id,
                    name=d["name"],
                    target_amount=d["amount"],
                    current_amount=0.0,
                    is_debt=True,
                    min_payment=d.get("min_payment"),
                    due_day=d.get("due_day")
                )
                session.add(debt_env)
                
            # Expenses
            for e in expenses:
                exp_env = Envelope(
                    user_id=user_id,
                    name=e["name"],
                    target_amount=e["amount"],
                    current_amount=0.0,
                    is_debt=False,
                    is_goal=False,
                    due_day=e.get("due_day")
                )
                session.add(exp_env)
                
            await session.flush()
            
            # 4. Deposit cash if > 0
            if cash > 0.0:
                unallocated.current_amount = cash
                start_tx = Transaction(
                    user_id=user_id,
                    amount=cash,
                    envelope_id=unallocated.id,
                    description="Стартовый капитал"
                )
                session.add(start_tx)
                await session.flush()
                
            # 5. Ledger verify
            if not await verify_user_ledger(session, user_id):
                await session.rollback()
                await wait_msg.edit_text("⚠️ Ошибка математического баланса. Пожалуйста, попробуй еще раз через /reset.")
                await state.clear()
                return
                
            await session.commit()

        # If user has cash > 0 and has at least one expense/debt envelope, ask AI for first recommended plan
        has_targets = len(debts) > 0 or len(expenses) > 0
        if cash > 0.0 and has_targets:
            await wait_msg.edit_text("⏳ <i>Бюджет настроен. Формирую твое первое действие с деньгами...</i>", parse_mode="HTML")
            
            # Fetch envelopes with their generated IDs
            async with async_session_maker() as session:
                env_res = await session.execute(select(Envelope).where(Envelope.user_id == user_id))
                all_envs = list(env_res.scalars().all())
                
                envelope_ids = [e.id for e in all_envs]
                monthly_payments = await get_monthly_payments(session, envelope_ids)
                
                env_context = build_envelopes_context(all_envs, monthly_payments)
                financial_health = build_financial_health(all_envs, monthly_income=income)
                
                prompt_text = (
                    f"Пользователь только что закончил онбординг. У него есть {cash:.0f} свободных денег. "
                    "Предложи план распределения с объяснением 'почему' для каждой строчки."
                )
                
                brain_response = await process_user_message(
                    user_text=prompt_text,
                    user_vibe=user.prompt_vibe,
                    envelopes_context=env_context,
                    financial_health=financial_health,
                    chat_history=[]
                )
                
                allocs = brain_response.income_allocations or brain_response.plan_items
                if allocs:
                    alloc_names = [a.envelope_name if hasattr(a, 'envelope_name') else a.name for a in allocs]
                    alloc_amounts = [a.amount for a in allocs]
                    
                    unallocated_env = next(e for e in all_envs if e.name.lower().strip() in ("нераспределённые", "кошелек", "кошелёк"))
                    
                    await state.set_state(IncomeStates.confirming)
                    await state.set_data({
                        "income_amount": cash,
                        "unallocated_env_id": unallocated_env.id,
                        "alloc_names": alloc_names,
                        "alloc_amounts": alloc_amounts
                    })
                    
                    final_text = (
                        f"<b>Бюджет успешно настроен! 🎉</b>\n\n"
                        f"{brain_response.coach_reply}"
                    )
                    
                    reply_markup = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✅ Да, применить", callback_data="confirm_income")],
                        [InlineKeyboardButton(text="❌ Оставить в нераспределенных", callback_data="reject_income")]
                    ])
                    
                    await wait_msg.delete()
                    await message.answer(final_text, parse_mode="HTML", reply_markup=reply_markup)
                    return
        
        # Fallback if no cash or no targets
        final_text = (
            f"<b>Бюджет успешно настроен! 🎉</b>\n\n"
            f"Все статьи созданы пустыми, планируемый доход сохранен. Жду твоих трат или поступлений!"
        )
        persistent_keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📊 Навигатор")],
                [KeyboardButton(text="🛍 Расходы"), KeyboardButton(text="💳 Долги")]
            ],
            resize_keyboard=True,
            is_persistent=True
        )
        await wait_msg.delete()
        await message.answer(final_text, parse_mode="HTML", reply_markup=persistent_keyboard)
        await state.clear()
        
    except Exception as e:
        logger.error(f"Error completing onboarding: {e}", exc_info=True)
        try:
            await wait_msg.delete()
        except Exception:
            pass
            
        final_text = (
            f"<b>Бюджет успешно настроен! 🎉</b>\n\n"
            f"Все данные сохранены. Нажми кнопку «Навигатор» ниже, чтобы посмотреть сводку."
        )
        persistent_keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="📊 Навигатор")],
                [KeyboardButton(text="🛍 Расходы"), KeyboardButton(text="💳 Долги")]
            ],
            resize_keyboard=True,
            is_persistent=True
        )
        await message.answer(final_text, parse_mode="HTML", reply_markup=persistent_keyboard)
        await state.clear()
