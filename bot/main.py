from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from . import __version__
from .config import Config
from .context import AppContext
from .handlers import router
from .health import start_health_server
from .keepalive import keepalive_loop
from .media import ensure_tools

log = logging.getLogger(__name__)

COMMANDS = [
    BotCommand(command="start", description="Как пользоваться ботом"),
    BotCommand(command="help", description="Поддержка и ограничения"),
    BotCommand(command="id", description="ID чата и пользователя"),
    BotCommand(command="ping", description="Проверка, что бот жив"),
]


async def run() -> None:
    cfg = Config.from_env()
    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("aiogram.event", "aiohttp.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if not cfg.token:
        raise SystemExit("TELEGRAM_TOKEN не задан. Добавьте переменную окружения и запустите снова.")

    ensure_tools()
    log.info("starting shorts-video-bot %s", __version__)

    app_ctx = AppContext(cfg=cfg, semaphore=asyncio.Semaphore(cfg.max_concurrent_jobs))
    bot = Bot(
        token=cfg.token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True),
    )
    dp = Dispatcher()
    dp["app"] = app_ctx
    dp.include_router(router)

    runner = await start_health_server(cfg.port, app_ctx)
    keepalive_task: asyncio.Task | None = None
    if cfg.keepalive_url:
        keepalive_task = asyncio.create_task(
            keepalive_loop(cfg.keepalive_url, cfg.keepalive_interval)
        )
    else:
        log.info("keepalive disabled (KEEPALIVE_URL/RENDER_EXTERNAL_URL not set)")
    try:
        me = await bot.get_me()
        log.info("authorized as @%s (id=%s)", me.username, me.id)
        try:
            await bot.set_my_commands(COMMANDS)
        except Exception:  # noqa: BLE001
            log.debug("could not set bot commands", exc_info=True)

        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if keepalive_task is not None:
            keepalive_task.cancel()
            try:
                await keepalive_task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                log.debug("keepalive task stopped with error", exc_info=True)
        await runner.cleanup()
        await bot.session.close()


def main() -> None:
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit) as exc:
        if isinstance(exc, SystemExit) and exc.code:
            raise
        log.info("stopped")


if __name__ == "__main__":
    main()
