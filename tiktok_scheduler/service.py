"""Top-level service that runs scheduler + Telegram bot + web UI together."""

from __future__ import annotations

import asyncio
import logging
import signal

import uvicorn

from .bot import build_bot, build_dispatcher
from .config import get_settings
from .db import init_db
from .scheduler import PostingWorker
from .web import create_app

log = logging.getLogger(__name__)


async def run_service() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = get_settings()
    await init_db()

    bot = build_bot(settings)
    dp = build_dispatcher()

    async def notify(text: str) -> None:
        for owner_id in settings.telegram_owner_ids:
            try:
                await bot.send_message(owner_id, text)
            except Exception:
                log.exception("Failed to notify owner %s", owner_id)

    worker = PostingWorker(settings=settings, notify=notify)
    worker_task = worker.start()

    bot_task = asyncio.create_task(
        dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()),
        name="telegram-bot",
    )

    web_task: asyncio.Task | None = None
    web_server: uvicorn.Server | None = None
    if settings.web_enabled:
        app = create_app()
        config = uvicorn.Config(
            app,
            host=settings.web_host,
            port=settings.web_port,
            log_level="info",
            loop="asyncio",
        )
        web_server = uvicorn.Server(config)
        web_task = asyncio.create_task(web_server.serve(), name="web-server")

    stop = asyncio.Event()

    def _request_stop(*_args):
        stop.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:  # pragma: no cover - windows
            signal.signal(sig, _request_stop)

    log.info("Service is up. Press Ctrl-C to stop.")
    await stop.wait()
    log.info("Stopping service...")

    if web_server is not None:
        web_server.should_exit = True
    bot_task.cancel()
    await worker.stop()
    for t in (bot_task, web_task, worker_task):
        if t is None:
            continue
        try:
            await t
        except (asyncio.CancelledError, Exception):
            pass

    await bot.session.close()
