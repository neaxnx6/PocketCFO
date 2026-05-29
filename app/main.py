import asyncio
import logging
from aiogram import Bot, Dispatcher
from fastapi import FastAPI

from app.config import settings
from app.bot.handlers import onboarding, transactions, family

logging.basicConfig(level=logging.WARNING)
logging.getLogger("app").setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# FastAPI app
app = FastAPI(title="На Балансе MVP")

# Bot Setup
if settings.BOT_TOKEN != "placeholder_token":
    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()

    dp.include_router(onboarding.router)
    dp.include_router(family.router)
    dp.include_router(transactions.router)
else:
    bot = None
    dp = None
    logger.warning("BOT_TOKEN is not set. The bot will not start.")

@app.get("/")
async def root():
    return {"status": "ok", "message": "На Балансе is running"}

async def start_polling():
    if bot and dp:
        logger.info("Starting Telegram Bot Polling...")
        from aiogram.types import BotCommand
        try:
            await bot.set_my_commands([
                BotCommand(command="start", description="Запустить помощника 🚀"),
                BotCommand(command="family", description="Семейный бюджет 👥"),
                BotCommand(command="reset", description="Сбросить все данные 🧹")
            ])
            await bot.set_my_description(
                "«На Балансе» — твой ИИ-финдиректор в стиле ретро-Sega. \n\n"
                "Управляй финансами играючи:\n"
                "• 🎤 Просто надиктуй свои доходы и расходы голосом.\n"
                "• 📊 Конверты: распределяй деньги на расходы, цели и долги.\n"
                "• 📅 Прогнозы: узнай точные сроки закрытия долгов и накоплений.\n"
                "• 👥 Семейный режим: веди бюджет вместе с партнером.\n\n"
                "Нажми кнопку «Запустить» внизу, чтобы начать! 🚀"
            )
            await bot.set_my_short_description(
                "ИИ-финдиректор в стиле ретро-Sega. Веди бюджет голосом, планируй конверты и закрывай долги вместе с партнером!"
            )
            logger.info("Bot commands and descriptions set successfully.")
        except Exception as e:
            logger.error(f"Failed to set bot commands: {e}")

        from app.services.nudge_service import run_nudge_scheduler
        asyncio.create_task(run_nudge_scheduler(bot))
        await dp.start_polling(bot, skip_updates=True)

if __name__ == "__main__":
    # If run directly as a script (python -m app.main), start polling
    try:
        asyncio.run(start_polling())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
