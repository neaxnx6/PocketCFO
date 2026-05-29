import random
import string
import logging
from typing import Optional
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy.future import select
from sqlalchemy import update

from app.database.session import async_session_maker
from app.database.models import User

router = Router()
logger = logging.getLogger(__name__)

class FamilyStates(StatesGroup):
    entering_code = State()

def generate_invite_code() -> str:
    """Generates a random code like CF-123456"""
    digits = ''.join(random.choices(string.digits, k=6))
    return f"CF-{digits}"

async def get_user_family_status(user: User, session, show_back_button: bool = False) -> tuple[str, Optional[InlineKeyboardMarkup]]:
    """Helper to format the family status message and build markup."""
    if user.family_host_id:
        # User is a member
        host_result = await session.execute(select(User).where(User.telegram_id == user.family_host_id))
        host = host_result.scalar_one_or_none()
        if host:
            text = (
                "👥 <b>Семейный бюджет</b>\n\n"
                "ℹ️ Ты подключен к семейному бюджету партнёра.\n"
                f"👤 <b>Партнёр (Владелец бюджета):</b> ID <code>{host.telegram_id}</code>\n"
                "💡 Вы используете общие конверты и общие настройки дохода владельца."
            )
            buttons = [
                [InlineKeyboardButton(text="🚪 Выйти из семейного бюджета", callback_data="family_leave")]
            ]
            if show_back_button:
                buttons.append([InlineKeyboardButton(text="⬅️ Назад к бюджету", callback_data="show_envelopes")])
            markup = InlineKeyboardMarkup(inline_keyboard=buttons)
            return text, markup
        else:
            # Host not found, reset
            user.family_host_id = None
            await session.flush()

    # Check if this user is a host (i.e. someone else has family_host_id pointing to this user)
    members_result = await session.execute(select(User).where(User.family_host_id == user.telegram_id))
    members = list(members_result.scalars().all())
    if members:
        member_ids = ", ".join([f"<code>{m.telegram_id}</code>" for m in members])
        text = (
            "👥 <b>Семейный бюджет (Режим Хоста)</b>\n\n"
            "ℹ️ Ты являешься владельцем семейного бюджета.\n"
            f"👥 <b>Подключенные партнёры:</b> {member_ids}\n"
            "💡 Все транзакции партнёров списываются из твоих конвертов."
        )
        buttons = [
            [InlineKeyboardButton(text="🚪 Распустить семейную группу", callback_data="family_leave")]
        ]
        if show_back_button:
            buttons.append([InlineKeyboardButton(text="⬅️ Назад к бюджету", callback_data="show_envelopes")])
        markup = InlineKeyboardMarkup(inline_keyboard=buttons)
        return text, markup

    # Solo mode
    code_str = f"<code>{user.invite_code}</code>" if user.invite_code else "не сгенерирован"
    text = (
        "👥 <b>Семейный бюджет</b>\n\n"
        "«На Балансе» позволяет объединить бюджеты с партнёром:\n"
        "• Совместные конверты расходов, целей и долгов\n"
        "• Общий финансовый дашборд и прогнозы\n"
        "• Раздельные чаты с ИИ (партнёр не увидит твою переписку)\n\n"
        f"🔑 <b>Твой код подключения:</b> {code_str}\n\n"
        "Если у партнёра уже есть код, нажми «Ввести код партнёра». Либо сгенерируй свой код и отправь его партнёру."
    )
    
    buttons = []
    if not user.invite_code:
        buttons.append([InlineKeyboardButton(text="🔑 Сгенерировать код", callback_data="family_gen_code")])
    else:
        buttons.append([InlineKeyboardButton(text="🔄 Сбросить код", callback_data="family_gen_code")])
        
    buttons.append([InlineKeyboardButton(text="📥 Ввести код партнёра", callback_data="family_enter_code")])
    if show_back_button:
        buttons.append([InlineKeyboardButton(text="⬅️ Назад к бюджету", callback_data="show_envelopes")])
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    return text, markup


def _should_show_back(callback: CallbackQuery) -> bool:
    if callback.message and callback.message.reply_markup and callback.message.reply_markup.inline_keyboard:
        for row in callback.message.reply_markup.inline_keyboard:
            for btn in row:
                if btn.callback_data == "show_envelopes":
                    return True
    return False


@router.message(Command("family"))
async def cmd_family(message: Message):
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = user_result.scalar_one_or_none()
        if not user:
            await message.answer("Сначала нажми /start")
            return
            
        text, markup = await get_user_family_status(user, session)
        await message.answer(text, parse_mode="HTML", reply_markup=markup)


@router.callback_query(F.data == "family_menu")
async def family_menu_callback(callback: CallbackQuery):
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = user_result.scalar_one_or_none()
        if not user:
            await callback.answer("Сначала нажми /start")
            return
            
        text, markup = await get_user_family_status(user, session, show_back_button=True)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await callback.answer()


@router.callback_query(F.data == "family_gen_code")
async def family_gen_code(callback: CallbackQuery):
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = user_result.scalar_one_or_none()
        if not user:
            await callback.answer("Пользователь не найден")
            return

        # Generate unique code
        for _ in range(10):
            code = generate_invite_code()
            dup_result = await session.execute(select(User).where(User.invite_code == code))
            if not dup_result.scalar_one_or_none():
                user.invite_code = code
                break
        
        await session.commit()
        show_back = _should_show_back(callback)
        text, markup = await get_user_family_status(user, session, show_back_button=show_back)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await callback.answer("Код подключения сгенерирован!")


@router.callback_query(F.data == "family_enter_code")
async def family_enter_code(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FamilyStates.entering_code)
    show_back = _should_show_back(callback)
    buttons = [[InlineKeyboardButton(text="❌ Отмена", callback_data="family_cancel")]]
    if show_back:
        buttons.append([InlineKeyboardButton(text="⬅️ Назад к бюджету", callback_data="show_envelopes")])
    cancel_markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(
        "📥 <b>Введи код партнёра</b>\n\n"
        "Отправь мне код партнёра в формате <code>CF-123456</code>:",
        parse_mode="HTML",
        reply_markup=cancel_markup
    )
    await callback.answer()


@router.callback_query(F.data == "family_cancel", FamilyStates.entering_code)
async def family_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = user_result.scalar_one_or_none()
        show_back = _should_show_back(callback)
        text, markup = await get_user_family_status(user, session, show_back_button=show_back)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await callback.answer("Ввод отменен")


@router.message(FamilyStates.entering_code)
async def process_invite_code(message: Message, state: FSMContext, bot: Bot):
    code = message.text.strip().upper()
    
    async with async_session_maker() as session:
        # Find code owner
        partner_result = await session.execute(select(User).where(User.invite_code == code))
        partner = partner_result.scalar_one_or_none()
        
        # Resolve current user
        user_result = await session.execute(select(User).where(User.telegram_id == message.from_user.id))
        user = user_result.scalar_one_or_none()
        
        if not partner:
            cancel_markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="family_cancel")]
            ])
            await message.answer(
                "❌ <b>Код не найден</b>\n"
                "Пожалуйста, проверь правильность ввода (формат: CF-XXXXXX) и попробуй ещё раз:",
                parse_mode="HTML",
                reply_markup=cancel_markup
            )
            return

        if partner.telegram_id == user.telegram_id:
            await message.answer("💡 Нельзя подключиться к самому себе. Введи код партнёра.")
            return

        # Connect!
        user.family_host_id = partner.telegram_id
        user.invite_code = None  # Reset own invite code
        await session.commit()
        await state.clear()
        
        # Notify user
        await message.answer(
            f"🎉 <b>Успешно!</b>\n\n"
            f"Ты подключился к семейному бюджету партнёра (ID <code>{partner.telegram_id}</code>).\n"
            f"Теперь вы используете общие конверты владельца. Свои соло-конверты временно скрыты.",
            parse_mode="HTML"
        )
        
        # Notify partner
        try:
            partner_name = message.from_user.full_name or f"ID {message.from_user.id}"
            await bot.send_message(
                partner.telegram_id,
                f"🎉 <b>Новый участник в бюджете!</b>\n\n"
                f"Пользователь {partner_name} (ID <code>{message.from_user.id}</code>) успешно подключился к твоему семейному бюджету!\n"
                f"Теперь вы ведёте финансы вместе.",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Could not notify partner {partner.telegram_id}: {e}")


@router.callback_query(F.data == "family_leave")
async def family_leave(callback: CallbackQuery, bot: Bot):
    async with async_session_maker() as session:
        user_result = await session.execute(select(User).where(User.telegram_id == callback.from_user.id))
        user = user_result.scalar_one_or_none()
        if not user:
            await callback.answer("Пользователь не найден")
            return

        partner_id = None
        is_host = False
        
        if user.family_host_id:
            partner_id = user.family_host_id
            user.family_host_id = None
        else:
            # User is a host, let's find members
            members_result = await session.execute(select(User).where(User.family_host_id == user.telegram_id))
            members = list(members_result.scalars().all())
            if members:
                is_host = True
                for m in members:
                    m.family_host_id = None
                    # Notify member
                    try:
                        await bot.send_message(
                            m.telegram_id,
                            "🚪 <b>Семейный бюджет распущен</b>\n\n"
                            "Владелец бюджета решил прекратить семейный режим. Ты возвращён в соло-режим.",
                            parse_mode="HTML"
                        )
                    except Exception as e:
                        logger.warning(f"Could not notify member {m.telegram_id}: {e}")

        await session.commit()
        
        if partner_id:
            try:
                user_name = callback.from_user.full_name or f"ID {callback.from_user.id}"
                await bot.send_message(
                    partner_id,
                    f"🚪 <b>Участник вышел из бюджета</b>\n\n"
                    f"Пользователь {user_name} отключился от твоего семейного бюджета.",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.warning(f"Could not notify partner {partner_id}: {e}")

        show_back = _should_show_back(callback)
        text, markup = await get_user_family_status(user, session, show_back_button=show_back)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
        await callback.answer("Вы успешно вышли из семейного режима")
