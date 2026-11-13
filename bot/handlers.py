from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message

from . import __version__
from .context import AppContext
from .downloader import DownloadFailed, download_media
from .formatting import build_caption, format_duration
from .media import MediaError, fit_for_telegram, make_thumbnail
from .urls import URL_RE, extract_links

log = logging.getLogger(__name__)
router = Router(name="links")

START_TEXT = (
    "Привет! Пришлите ссылку на <b>YouTube Shorts</b>, <b>Instagram Reels</b> "
    "или <b>TikTok</b> — я скачаю видео в хорошем качестве и отправлю его сюда.\n\n"
    "Можно отправлять до {limit} ссылок в одном сообщении.\n"
    "Команды: /help — подробности, /id — ID чата."
)

HELP_TEXT = (
    "<b>Как пользоваться</b>\n"
    "1. Скопируйте ссылку на видео.\n"
    "2. Отправьте её в чат.\n"
    "3. Подождите — бот пришлёт видеофайл.\n\n"
    "<b>Поддерживаются</b>\n"
    "• youtube.com/shorts/…, youtu.be/…\n"
    "• instagram.com/reel/…, /p/…\n"
    "• tiktok.com/…, vm.tiktok.com/…\n\n"
    "<b>Ограничения</b>\n"
    "• Видео до {minutes} минут и до {size} МБ после обработки.\n"
    "• TikTok скачивается без водяного знака (если доступен).\n"
    "• Для приватных видео Instagram нужны cookies — их задаёт админ бота.\n\n"
    "<b>Важно для групп</b>\n"
    "Чтобы бот видел ссылки в группе, отключите Privacy Mode: @BotFather → /setprivacy → "
    "выберите бота → Disable. После этого перезайдите бота в группу."
)


def _is_admin(app: AppContext, message: Message) -> bool:
    user = message.from_user
    return bool(user and app.cfg.admin_ids and user.id in app.cfg.admin_ids)


def _chat_allowed(app: AppContext, message: Message) -> bool:
    if not app.cfg.allowed_chat_ids:
        return True
    if message.chat.id in app.cfg.allowed_chat_ids:
        return True
    return _is_admin(app, message)


@router.message(CommandStart())
async def cmd_start(message: Message, app: AppContext) -> None:
    await message.answer(START_TEXT.format(limit=app.cfg.max_links_per_message))


@router.message(Command("help"))
async def cmd_help(message: Message, app: AppContext) -> None:
    await message.answer(
        HELP_TEXT.format(
            minutes=max(1, app.cfg.max_duration_sec // 60),
            size=round(app.cfg.max_upload_bytes / (1024 * 1024)),
        )
    )


@router.message(Command("id"))
async def cmd_id(message: Message) -> None:
    user_id = message.from_user.id if message.from_user else "—"
    await message.answer(f"Чат: <code>{message.chat.id}</code>\nПользователь: <code>{user_id}</code>")


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    await message.answer("pong 🏓")


@router.message(Command("stats"))
async def cmd_stats(message: Message, app: AppContext) -> None:
    if not _is_admin(app, message):
        return
    await message.answer(
        f"<b>Статистика</b>\n"
        f"Версия: {__version__}\n"
        f"Аптайм: {format_duration(app.uptime_sec)}\n"
        f"Обработано: {app.done}\n"
        f"Ошибок: {app.failed}\n"
        f"В работе: {app.active}\n"
        f"В очереди: {app.pending}"
    )


async def _safe_edit(status: Message | None, text: str) -> None:
    if status is None:
        return
    try:
        await status.edit_text(text, parse_mode=None)
    except TelegramBadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            log.debug("status edit failed: %s", exc)


async def _report(status: Message | None, message: Message, text: str) -> None:
    if status is not None:
        await _safe_edit(status, text)
        return
    try:
        await message.reply(text, parse_mode=None)
    except TelegramBadRequest:
        log.debug("failed to send error report")


async def _send_video(
    message: Message,
    fitted,
    thumb: Path | None,
    caption: str,
) -> None:
    kwargs: dict = {
        "video": FSInputFile(fitted.path),
        "caption": caption,
        "parse_mode": "HTML",
        "supports_streaming": True,
    }
    duration = int(round(fitted.info.duration or 0))
    if duration > 0:
        kwargs["duration"] = duration
    if fitted.info.width > 0 and fitted.info.height > 0:
        kwargs["width"] = fitted.info.width
        kwargs["height"] = fitted.info.height
    if thumb is not None:
        kwargs["thumbnail"] = FSInputFile(thumb)

    try:
        await message.reply_video(**kwargs)
    except TelegramBadRequest as exc:
        # Исходное сообщение могли удалить, либо reply не разрешён — отправляем без reply.
        if "reply" in str(exc).lower() or "message_id_invalid" in str(exc).lower():
            await message.bot.send_video(chat_id=message.chat.id, **kwargs)
        else:
            raise


async def _process_link(bot: Bot, app: AppContext, message: Message, url: str) -> None:
    cfg = app.cfg
    app.pending = max(0, app.pending - 1)
    job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=str(cfg.work_dir)))
    status: Message | None = None
    try:
        try:
            status = await message.reply("⏳ В очереди…")
        except TelegramBadRequest:
            status = None

        async with app.semaphore:
            app.active += 1
            try:
                await _safe_edit(status, "⬇️ Скачиваю видео…")
                path, meta = await asyncio.to_thread(download_media, url, job_dir, cfg)

                await _safe_edit(status, "🎞 Готовлю видео для Telegram…")
                fitted = await asyncio.to_thread(
                    fit_for_telegram, path, job_dir, cfg.max_upload_bytes
                )
                thumb = await asyncio.to_thread(
                    make_thumbnail, fitted.path, job_dir, fitted.info.duration
                )

                caption = build_caption(
                    title=meta.get("title") or "Видео",
                    uploader=meta.get("uploader") or "",
                    url=meta.get("webpage_url") or url,
                    duration=fitted.info.duration,
                    height=fitted.info.height,
                )
                await _safe_edit(status, "📤 Отправляю…")
                await _send_video(message, fitted, thumb, caption)

                app.done += 1
                if status is not None:
                    try:
                        await status.delete()
                    except TelegramBadRequest:
                        pass
                log.info("sent video for %s (%s bytes)", url, fitted.info.size)
            finally:
                app.active -= 1

    except DownloadFailed as exc:
        app.failed += 1
        await _report(status, message, f"❌ {exc}")
    except MediaError as exc:
        app.failed += 1
        await _report(status, message, f"❌ {exc}")
    except TelegramForbiddenError:
        app.failed += 1
        log.warning("bot was blocked or kicked in chat %s", message.chat.id)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        app.failed += 1
        log.exception("failed to process %s", url)
        await _report(status, message, "❌ Что-то пошло не так. Попробуйте ещё раз.")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)


@router.message(F.text | F.caption)
async def on_message(message: Message, bot: Bot, app: AppContext) -> None:
    if message.chat.type == ChatType.CHANNEL:
        return
    if not _chat_allowed(app, message):
        return

    text = message.text or message.caption or ""
    if text.lstrip().startswith("/"):
        return

    links = extract_links(text, app.cfg.allowed_hosts)
    if not links:
        # В личке подсказываем, если человек явно прислал какую-то ссылку.
        if message.chat.type == ChatType.PRIVATE and URL_RE.search(text):
            await message.reply(
                "Эта ссылка не поддерживается. Нужен YouTube Shorts, Instagram Reels или TikTok."
            )
        return

    links = links[: app.cfg.max_links_per_message]
    if app.pending >= app.cfg.max_queue:
        await message.reply(
            "⏳ Слишком много запросов в очереди. Подождите немного и отправьте ссылку снова."
        )
        return

    for url in links:
        app.pending += 1
        task = asyncio.create_task(_process_link(bot, app, message, url))
        app.tasks.add(task)
        task.add_done_callback(app.tasks.discard)
