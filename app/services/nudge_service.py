import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import select, func, and_
from aiogram import Bot

from app.database.session import async_session_maker
from app.database.models import User, Envelope, Transaction, UserNudge

logger = logging.getLogger(__name__)

# Порог предупреждения о низком балансе категории (20%)
LOW_BALANCE_THRESHOLD = 0.20


def fmt_money(val: float) -> str:
    """Format money: 216750 -> '216.8к', 5000 -> '5к', 1500.5 -> '1.5к'"""
    if val >= 1000:
        return f"{val/1000:.1f}к".replace('.0к', 'к')
    return f"{val:.0f}"


async def check_and_send_nudges(bot: Bot):
    """
    Сканирует базу данных и отправляет персонализированные пуши пользователям.
    """
    logger.info("Running proactive nudge check...")
    async with async_session_maker() as session:
        # Получаем всех активных пользователей
        user_result = await session.execute(select(User))
        users = user_result.scalars().all()

        for user in users:
            try:
                # 0. Определяем владельца бюджета
                budget_owner = user
                if user.family_host_id:
                    host_result = await session.execute(select(User).where(User.telegram_id == user.family_host_id))
                    host = host_result.scalar_one_or_none()
                    if host:
                        budget_owner = host
                    else:
                        user.family_host_id = None
                        await session.flush()

                # Получаем все конверты владельца бюджета
                env_result = await session.execute(
                    select(Envelope).where(Envelope.user_id == budget_owner.telegram_id)
                )
                envelopes = list(env_result.scalars().all())

                if not envelopes:
                    continue

                # 1. Проверка Re-engagement (напоминание о забытом бюджете)
                await check_re_engagement(session, bot, user, envelopes)

                # 2. Проверка Low Balance (предупреждения о низком лимите)
                await check_low_balances(session, bot, user, envelopes)

                # 3. Проверка Positive Reinforcement (прогресс по долгам)
                await check_debt_progress(session, bot, user, budget_owner)

            except Exception as e:
                logger.error(f"Error checking nudges for user {user.telegram_id}: {e}", exc_info=True)


async def check_re_engagement(session, bot: Bot, user: User, envelopes: list):
    # Ищем последнюю транзакцию пользователя
    tx_result = await session.execute(
        select(Transaction)
        .where(Transaction.user_id == user.telegram_id)
        .order_by(Transaction.datetime_created.desc())
        .limit(1)
    )
    last_tx = tx_result.scalar_one_or_none()

    if not last_tx:
        return  # Нет транзакций — пользователь еще не начал вести бюджет

    now = datetime.utcnow()
    # Если последняя транзакция была более 48 часов назад
    if now - last_tx.datetime_created > timedelta(hours=48):
        # Проверяем, слали ли мы такое уведомление в последние 48 часов
        nudge_result = await session.execute(
            select(UserNudge)
            .where(
                and_(
                    UserNudge.user_id == user.telegram_id,
                    UserNudge.nudge_type == "re_engagement",
                    UserNudge.datetime_sent > now - timedelta(hours=48)
                )
            )
        )
        already_sent = nudge_result.scalar_one_or_none()

        if not already_sent:
            msg_text = "Ты давно не обновлял свои расходы. Продолжим вести бюджет? ☕️"
            try:
                await bot.send_message(user.telegram_id, msg_text)
                # Логируем отправку
                session.add(UserNudge(user_id=user.telegram_id, nudge_type="re_engagement"))
                await session.commit()
                logger.info(f"Sent re-engagement nudge to user {user.telegram_id}")
            except Exception as e:
                logger.warning(f"Failed to send message to user {user.telegram_id}: {e}")


async def check_low_balances(session, bot: Bot, user: User, envelopes: list):
    # Ищем расходные категории (не долги, не цели, не буфер, не нераспределенные)
    expense_envs = [
        e for e in envelopes 
        if not getattr(e, 'is_debt', False) 
        and not getattr(e, 'is_goal', False) 
        and "буфер" not in e.name.lower()
        and e.name.lower().strip() not in ("нераспределённые", "кошелек", "кошелёк")
    ]

    now = datetime.utcnow()
    # Проверяем баланс каждой категории
    for env in expense_envs:
        if not env.target_amount or env.target_amount <= 0:
            continue

        pct_left = env.current_amount / env.target_amount
        if pct_left < LOW_BALANCE_THRESHOLD:
            # Проверяем, предупреждали ли мы уже в этом календарном месяце по этому конверту
            start_of_month = datetime(now.year, now.month, 1)
            nudge_result = await session.execute(
                select(UserNudge)
                .where(
                    and_(
                        UserNudge.user_id == user.telegram_id,
                        UserNudge.nudge_type == "low_balance",
                        UserNudge.envelope_id == env.id,
                        UserNudge.datetime_sent >= start_of_month
                    )
                )
            )
            already_sent = nudge_result.scalar_one_or_none()

            if not already_sent:
                remaining_pct = int(pct_left * 100)
                msg_text = (
                    f"⚠️ <b>Внимание:</b> В категории «{env.name}» осталось всего "
                    f"<b>{fmt_money(env.current_amount)} руб.</b> (около {remaining_pct}% лимита)."
                )
                try:
                    await bot.send_message(user.telegram_id, msg_text, parse_mode="HTML")
                    session.add(UserNudge(
                        user_id=user.telegram_id, 
                        nudge_type="low_balance", 
                        envelope_id=env.id
                    ))
                    await session.commit()
                    logger.info(f"Sent low_balance nudge for envelope {env.id} to user {user.telegram_id}")
                except Exception as e:
                    logger.warning(f"Failed to send low_balance nudge to user {user.telegram_id}: {e}")


async def check_debt_progress(session, bot: Bot, user: User, budget_owner: User):
    # Проверяем уменьшение долгов за последние 7 дней
    # Ищем транзакции пользователя по конвертам долгов за 7 дней
    now = datetime.utcnow()
    nudge_result = await session.execute(
        select(UserNudge)
        .where(
            and_(
                UserNudge.user_id == user.telegram_id,
                UserNudge.nudge_type == "debt_progress",
                UserNudge.datetime_sent > now - timedelta(days=7)
            )
        )
    )
    already_sent = nudge_result.scalar_one_or_none()
    if already_sent:
        return

    # Находим все конверты-долги владельца бюджета
    env_result = await session.execute(
        select(Envelope).where(
            and_(Envelope.user_id == budget_owner.telegram_id, Envelope.is_debt == True)
        )
    )
    debt_envs = env_result.scalars().all()
    if not debt_envs:
        return

    # Считаем сумму пополнений (транзакций с положительным amount) в эти конверты за неделю от любого члена семьи
    debt_env_ids = [d.id for d in debt_envs]
    
    family_user_ids = [budget_owner.telegram_id]
    member_result = await session.execute(
        select(User.telegram_id).where(User.family_host_id == budget_owner.telegram_id)
    )
    family_user_ids.extend(member_result.scalars().all())

    tx_result = await session.execute(
        select(func.sum(Transaction.amount))
        .where(
            and_(
                Transaction.user_id.in_(family_user_ids),
                Transaction.envelope_id.in_(debt_env_ids),
                Transaction.amount > 0,
                Transaction.datetime_created > now - timedelta(days=7)
            )
        )
    )
    repaid_amount = tx_result.scalar() or 0.0

    if repaid_amount >= 5000:  # Заметный прогресс от 5000 руб
        msg_text = (
            f"🔥 <b>Отличный прогресс:</b> За последнюю неделю ты сократил свои долги на "
            f"<b>{fmt_money(repaid_amount)} руб.</b> Так держать! Каждое гашение приближает тебя к свободе. 💪"
        )
        try:
            await bot.send_message(user.telegram_id, msg_text, parse_mode="HTML")
            session.add(UserNudge(user_id=user.telegram_id, nudge_type="debt_progress"))
            await session.commit()
            logger.info(f"Sent debt_progress nudge to user {user.telegram_id}")
        except Exception as e:
            logger.warning(f"Failed to send debt_progress nudge to user {user.telegram_id}: {e}")


async def run_nudge_scheduler(bot: Bot):
    """
    Фоновый бесконечный цикл шедулера пушей.
    """
    logger.info("Background nudge scheduler started.")
    # Ждем 30 секунд при старте, чтобы бот успел запуститься
    await asyncio.sleep(30)
    while True:
        try:
            await check_and_send_nudges(bot)
        except Exception as e:
            logger.error(f"Critical error in nudge scheduler loop: {e}", exc_info=True)
        
        # Сканируем раз в 4 часа (14400 секунд), чтобы не перегружать сервер
        await asyncio.sleep(14400)
