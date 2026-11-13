from __future__ import annotations

import logging
import re
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError

from .config import Config

log = logging.getLogger(__name__)

VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}

# Предпочитаем H.264/AAC: такие файлы Telegram не пережимает и играет на любом устройстве.
# Порядок важен:
#   1. лучшее H.264-видео (avc1 у YouTube, h264 у TikTok) + AAC-аудио;
#   2. готовый MP4 с видео и звуком (Instagram отдаёт такие без кодека в метаданных);
#   3. лучшее комбинированное;
#   4. любые дорожки (потом перекодируем в H.264);
#   5. что угодно.
FORMAT_SPEC = (
    "bv*[height<=1920][vcodec~='^(avc1|h264)']+ba[acodec~='^(mp4a|aac)']"
    "/b[height<=1920][vcodec~='^(avc1|h264)']"
    "/b"
    "/bv*[height<=1920]+ba"
    "/bv*+ba"
)


class DownloadFailed(Exception):
    """Ошибка скачивания с сообщением для пользователя."""


class _SilentLogger:
    def debug(self, msg: str) -> None:  # noqa: D102
        pass

    def info(self, msg: str) -> None:  # noqa: D102
        pass

    def warning(self, msg: str) -> None:  # noqa: D102
        pass

    def error(self, msg: str) -> None:  # noqa: D102
        log.warning("yt-dlp: %s", msg)


def _pick_file(work_dir: Path, info: dict) -> Path | None:
    candidates: list[Path] = []
    downloads = info.get("requested_downloads") or []
    for item in downloads:
        raw = item.get("filepath") or item.get("_filename")
        if raw:
            path = Path(raw)
            if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES:
                candidates.append(path)
    for path in work_dir.rglob("*"):
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES:
            candidates.append(path)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_size)


def _friendly_error(raw: str) -> str:
    text = raw.lower()
    if "sign in to confirm" in text or "not a bot" in text:
        return (
            "YouTube заблокировал скачивание с сервера (проверка «я не бот»). "
            "Попробуйте позже или добавьте cookies/прокси в настройках бота."
        )
    if "login required" in text or "rate-limit" in text or "empty media response" in text:
        return (
            "Instagram требует вход в аккаунт для этого видео. "
            "Добавьте cookies аккаунта в настройках бота и попробуйте снова."
        )
    if "private" in text or "unavailable" in text or "not available" in text:
        return "Видео недоступно: возможно, оно удалено или закрыто приватностью."
    if "unsupported url" in text:
        return "Эта ссылка не поддерживается. Нужен YouTube Shorts, Reels или TikTok."
    if "http error 404" in text or "not found" in text:
        return "Видео не найдено — проверьте ссылку."
    if "max-filesize" in text or "larger than max" in text:
        return "Видео больше допустимого размера."
    if "timed out" in text or "timeout" in text:
        return "Сервис не ответил вовремя. Попробуйте ещё раз."
    if "geo" in text and "restrict" in text:
        return "Видео заблокировано в регионе сервера. Поможет прокси в настройках бота."
    cleaned = re.sub(r"\s+", " ", raw).strip()
    return f"Не удалось скачать видео: {cleaned[:180]}" if cleaned else "Не удалось скачать видео."


def download_media(url: str, work_dir: Path, cfg: Config) -> tuple[Path, dict]:
    """Скачивает лучшее доступное видео. Блокирующая функция — вызывать в отдельном потоке."""
    work_dir.mkdir(parents=True, exist_ok=True)

    def match_filter(info: dict, *, incomplete: bool = False) -> str | None:
        duration = info.get("duration")
        if duration and duration > cfg.max_duration_sec:
            minutes = int(cfg.max_duration_sec // 60)
            return f"Слишком длинное видео (лимит {minutes} мин)."
        return None

    options: dict = {
        "format": FORMAT_SPEC,
        "merge_output_format": "mp4",
        "paths": {"home": str(work_dir), "temp": str(work_dir)},
        "outtmpl": "%(id)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _SilentLogger(),
        "restrictfilenames": True,
        "windowsfilenames": True,
        "overwrites": True,
        "retries": 3,
        "fragment_retries": 3,
        "socket_timeout": 25,
        "concurrent_fragment_downloads": 4,
        "max_filesize": cfg.max_download_bytes,
        "match_filter": match_filter,
        "http_headers": {"Accept-Language": "en-US,en;q=0.9"},
        "postprocessors": [],
    }
    if cfg.cookies_path:
        options["cookiefile"] = cfg.cookies_path
    if cfg.proxy:
        options["proxy"] = cfg.proxy

    log.info("download: %s", url)
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
        if not info:
            raise DownloadFailed("Не удалось получить информацию о видео.")
        if info.get("_type") == "playlist":
            entries = [entry for entry in info.get("entries") or [] if entry]
            if not entries:
                raise DownloadFailed("В ссылке не нашлось видео.")
            info = entries[0]
        path = _pick_file(work_dir, info)
        if not path or path.stat().st_size == 0:
            raise DownloadFailed("Видео скачалось пустым. Попробуйте другую ссылку.")
        meta = {
            "title": (info.get("title") or "").strip(),
            "uploader": (info.get("uploader") or info.get("channel") or "").strip(),
            "webpage_url": info.get("webpage_url") or url,
            "extractor": (info.get("extractor_key") or "").strip(),
            "format_id": (info.get("format_id") or "").strip(),
        }
        log.info(
            "downloaded %s (%s bytes, format=%s)",
            path.name,
            path.stat().st_size,
            meta["format_id"] or "?",
        )
        return path, meta
    except DownloadFailed:
        raise
    except DownloadError as exc:
        log.warning("yt-dlp error for %s: %s", url, exc)
        raise DownloadFailed(_friendly_error(str(exc))) from exc
    except Exception as exc:  # noqa: BLE001
        log.exception("unexpected download error for %s", url)
        raise DownloadFailed("Неожиданная ошибка при скачивании. Попробуйте ещё раз.") from exc
