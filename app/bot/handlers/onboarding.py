from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.types import Message
from aiogram.fsm.context import FSMContext
from sqlalchemy.future import select
from sqlalchemy import delete, update
from app.database.session import async_session_maker
from app.database.models import User, Envelope, Transaction, ChatMessage

router = Router()

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
        
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
    persistent_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Навигатор")],
            [KeyboardButton(text="🛍 Расходы"), KeyboardButton(text="💳 Долги")]
        ],
        resize_keyboard=True,
        is_persistent=True
    )
    await message.answer("🧹 Все твои данные, конверты и история памяти полностью удалены! Напиши /start, чтобы начать с чистого листа.", reply_markup=persistent_keyboard)

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext = None):
    if state:
        await state.clear()
    async with async_session_maker() as session:
        result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = result.scalar_one_or_none()
        
        if not user:
            user = User(telegram_id=message.from_user.id)
            session.add(user)
            await session.commit()
            
            welcome_text = (
                "👋 Привет! Я «На Балансе» — твой ИИ-помощник по финансам.\n\n"
                "<b>Как работаем:</b>\n\n"
                "🎤 Запиши голосовое или напиши текстом: сколько зарабатываешь, какие есть обязательные платежи, долги и на что копишь. Я составлю финансовый план и создам фонды.\n\n"
                "<i>Или просто начни писать расходы — разберёмся по ходу.</i>"
            )
        else:
            welcome_text = "С возвращением! Я помню все твои цели. Жду расходы, доходы или вопросы! (Если хочешь начать жизнь с чистого листа, нажми /reset)"
            
    from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, FSInputFile
    persistent_keyboard = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📊 Навигатор")],
            [KeyboardButton(text="🛍 Расходы"), KeyboardButton(text="💳 Долги")]
        ],
        resize_keyboard=True,
        is_persistent=True
    )
    
    import os
    # Root of the project
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
        except Exception as e:
            # Fallback to text in case of TG sending error
            pass
            
    await message.answer(welcome_text, parse_mode="HTML", reply_markup=persistent_keyboard)
