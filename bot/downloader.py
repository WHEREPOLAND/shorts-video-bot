from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError

from .config import Config
from .urls import host_of

log = logging.getLogger(__name__)

VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv"}

# Предпочитаем H.264/AAC: такие файлы Telegram не пережимает и играет на любом устройстве.
# Порядок важен:
#   1. лучшее H.264-видео (avc1 у YouTube, h264 у TikTok) + AAC-аудио;
#   2. готовый MP4 с видео и звуком (Instagram отдаёт такие без кодека в метаданных);
#   3. лучшее комбинированное (меньше склейки — быстрее отправка);
#   4. любые дорожки (потом перекодируем в H.264);
#   5. что угодно.
def format_spec(max_height: int = 1080) -> str:
    height = max(360, int(max_height))
    return (
        f"bv*[height<={height}][vcodec~='^(avc1|h264)']+ba[acodec~='^(mp4a|aac)']"
        f"/b[height<={height}][vcodec~='^(avc1|h264)']"
        f"/b[height<={height}]"
        # Instagram отдаёт progressive-форматы без высоты и кодека в метаданных.
        "/b"
        f"/bv*[height<={height}]+ba"
        f"/bv*+ba"
    )


FORMAT_SPEC = format_spec(1080)

# Если YouTube включает проверку «я не бот», пробуем другие клиенты плеера.
# web_embedded обычно отдаёт полное качество, android_vr — 360p, но часто проходит.
YOUTUBE_CLIENTS: tuple[str | None, ...] = (None, "web_embedded", "android_vr", "tv_embedded")

_RETRY_MARKERS = (
    "sign in to confirm",
    "not a bot",
    "page needs to be reloaded",
    "failed to extract",
    "unable to extract",
    "no video formats",
    "requested format is not available",
    "unable to download",
    "http error 403",
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


def _is_youtube(url: str) -> bool:
    host = host_of(url)
    return host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com")


def _is_retryable(raw: str) -> bool:
    text = raw.lower()
    return any(marker in text for marker in _RETRY_MARKERS)


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
    if "слишком длинное" in text:
        return "Видео слишком длинное для обработки."
    if "timed out" in text or "timeout" in text:
        return "Сервис не ответил вовремя. Попробуйте ещё раз."
    if "geo" in text and "restrict" in text:
        return "Видео заблокировано в регионе сервера. Поможет прокси в настройках бота."
    cleaned = re.sub(r"\s+", " ", raw).strip()
    return f"Не удалось скачать видео: {cleaned[:180]}" if cleaned else "Не удалось скачать видео."


def _build_options(
    cfg: Config,
    target_dir: Path,
    extra: dict | None = None,
    *,
    use_aria2: bool = False,
) -> dict:
    def match_filter(info: dict, *, incomplete: bool = False) -> str | None:
        duration = info.get("duration")
        if duration and duration > cfg.max_duration_sec:
            minutes = int(cfg.max_duration_sec // 60)
            return f"Слишком длинное видео (лимит {minutes} мин)."
        return None

    options: dict = {
        "format": format_spec(cfg.max_height),
        "merge_output_format": "mp4",
        "paths": {"home": str(target_dir), "temp": str(target_dir)},
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
        # Параллельные фрагменты ускоряют HLS/DASH-источники (YouTube, часть CDN).
        "concurrent_fragment_downloads": 8,
        "max_filesize": cfg.max_download_bytes,
        "match_filter": match_filter,
        "http_headers": {"Accept-Language": "en-US,en;q=0.9"},
    }
    if cfg.cookies_path:
        options["cookiefile"] = cfg.cookies_path
    if cfg.proxy:
        options["proxy"] = cfg.proxy
    if use_aria2:
        # aria2 качает файл в несколько соединений — заметно быстрее на медленных CDN.
        options["external_downloader"] = {"default": "aria2c"}
        options["external_downloader_args"] = {
            "default": [
                "-x",
                "8",
                "-s",
                "8",
                "-k",
                "1M",
                "--summary-interval=0",
                "--console-log-level=error",
            ]
        }
    if extra:
        options.update(extra)
    return options


def _download_once(
    url: str,
    target_dir: Path,
    cfg: Config,
    client: str | None,
    *,
    use_aria2: bool = False,
) -> tuple[Path, dict]:
    target_dir.mkdir(parents=True, exist_ok=True)
    extra = None
    if client:
        extra = {"extractor_args": {"youtube": {"player_client": [client]}}}
    options = _build_options(cfg, target_dir, extra, use_aria2=use_aria2)

    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
    if not info:
        raise DownloadFailed("Не удалось получить информацию о видео.")
    if info.get("_type") == "playlist":
        entries = [entry for entry in info.get("entries") or [] if entry]
        if not entries:
            raise DownloadFailed("В ссылке не нашлось видео.")
        info = entries[0]
    path = _pick_file(target_dir, info)
    if not path or path.stat().st_size == 0:
        raise DownloadFailed("Видео скачалось пустым. Попробуйте другую ссылку.")
    downloads = info.get("requested_downloads") or []
    actual = downloads[0] if downloads else {}

    def first(*keys: str) -> object:
        for key in keys:
            for source in (actual, info):
                value = source.get(key) if isinstance(source, dict) else None
                if value not in (None, "", 0, 0.0):
                    return value
        return None

    def as_float(value: object) -> float:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    def as_int(value: object) -> int:
        try:
            return int(float(value))  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0

    def as_codec(value: object) -> str:
        text = str(value or "").strip().lower()
        return "" if text in {"none", "unknown", ""} else text

    meta = {
        "title": (info.get("title") or "").strip(),
        "uploader": (info.get("uploader") or info.get("channel") or "").strip(),
        "webpage_url": info.get("webpage_url") or url,
        "extractor": (info.get("extractor_key") or "").strip(),
        "format_id": (info.get("format_id") or "").strip(),
        "duration": as_float(first("duration")),
        "width": as_int(first("width")),
        "height": as_int(first("height")),
        "fps": as_float(first("fps")),
        "vcodec": as_codec(first("vcodec")),
        "acodec": as_codec(first("acodec")),
    }
    return path, meta


def download_media(url: str, work_dir: Path, cfg: Config) -> tuple[Path, dict]:
    """Скачивает лучшее доступное видео. Блокирующая функция — вызывать в отдельном потоке."""
    work_dir.mkdir(parents=True, exist_ok=True)
    clients = YOUTUBE_CLIENTS if _is_youtube(url) else (None,)
    use_aria2 = cfg.aria2 and not _is_youtube(url) and shutil.which("aria2c") is not None

    # (клиент плеера, aria2) — сначала быстро, потом обычным способом.
    plan: list[tuple[str | None, bool]] = [(client, use_aria2) for client in clients]
    if use_aria2:
        plan.append((None, False))

    first_error = ""
    last_error = ""
    result: tuple[Path, dict] | None = None
    used_client: str | None = None
    used_aria2 = False

    for index, (client, aria2) in enumerate(plan):
        target = work_dir / f"try{index + 1}"
        log.info("download: %s (client=%s, aria2=%s)", url, client or "default", aria2)
        try:
            result = _download_once(url, target, cfg, client, use_aria2=aria2)
            used_client = client
            used_aria2 = aria2
            break
        except DownloadError as exc:
            last_error = str(exc)
            if not first_error:
                first_error = last_error
            log.warning("yt-dlp error for %s [%s]: %s", url, client or "default", last_error[:300])
            if aria2:
                # Многопоточная загрузка не подошла — пробуем обычную.
                continue
            if client is None and len(plan) == 1:
                break
            if not _is_retryable(last_error):
                break
        except DownloadFailed as exc:
            log.warning("download failed for %s: %s", url, exc)
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("unexpected download error for %s", url)
            raise DownloadFailed("Неожиданная ошибка при скачивании. Попробуйте ещё раз.") from exc

    if result is None:
        raise DownloadFailed(_friendly_error(first_error or last_error))

    path, meta = result
    log.info(
        "downloaded %s (%.1f MB, format=%s, client=%s, aria2=%s)",
        path.name,
        path.stat().st_size / 1048576,
        meta["format_id"] or "?",
        used_client or "default",
        used_aria2,
    )
    return path, meta
