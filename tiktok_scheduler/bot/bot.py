"""aiogram bot factory + entrypoint."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from ..config import Settings, get_settings
from .handlers import router

log = logging.getLogger(__name__)


def build_bot(settings: Settings | None = None) -> Bot:
    settings = settings or get_settings()
    if not settings.telegram_bot_token:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not set. Create a bot via @BotFather and put "
            "the token into your .env file."
        )
    return Bot(
        settings.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    return dp


async def run_bot() -> None:
    settings = get_settings()
    bot = build_bot(settings)
    dp = build_dispatcher()
    log.info("Starting Telegram bot polling")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
