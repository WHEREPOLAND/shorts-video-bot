from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_ALLOWED_HOSTS = (
    "youtube.com",
    "youtu.be",
    "instagram.com",
    "instagr.am",
    "tiktok.com",
)


def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_set(name: str) -> frozenset[int]:
    raw = _env_str(name)
    if not raw:
        return frozenset()
    result: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            result.add(int(chunk))
    return frozenset(result)


def _env_hosts(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = _env_str(name)
    if not raw:
        return default
    hosts = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return hosts or default


@dataclass(slots=True)
class Config:
    token: str
    admin_ids: frozenset[int] = field(default_factory=frozenset)
    allowed_chat_ids: frozenset[int] = field(default_factory=frozenset)
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS
    port: int = 8080
    work_dir: Path = Path("/tmp/shorts-video-bot")
    max_upload_mb: int = 49
    max_download_mb: int = 512
    max_duration_sec: int = 600
    max_concurrent_jobs: int = 1
    max_queue: int = 10
    max_links_per_message: int = 3
    proxy: str | None = None
    cookies_file: str | None = None
    log_level: str = "INFO"

    @property
    def max_upload_bytes(self) -> int:
        # Telegram Bot API (облачный) принимает файлы ботов до 50 МБ.
        mb = max(5, min(self.max_upload_mb, 49))
        return mb * 1024 * 1024

    @property
    def max_download_bytes(self) -> int:
        return max(1, self.max_download_mb) * 1024 * 1024

    @classmethod
    def from_env(cls) -> "Config":
        work_dir = Path(_env_str("WORK_DIR", "/tmp/shorts-video-bot"))
        cfg = cls(
            token=_env_str("TELEGRAM_TOKEN"),
            admin_ids=_env_set("ADMIN_IDS"),
            allowed_chat_ids=_env_set("ALLOWED_CHAT_IDS"),
            allowed_hosts=_env_hosts("ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS),
            port=_env_int("PORT", 8080),
            work_dir=work_dir,
            max_upload_mb=_env_int("MAX_UPLOAD_MB", 49),
            max_download_mb=_env_int("MAX_DOWNLOAD_MB", 512),
            max_duration_sec=_env_int("MAX_DURATION_SEC", 600),
            max_concurrent_jobs=max(1, _env_int("MAX_CONCURRENT_JOBS", 1)),
            max_queue=max(1, _env_int("MAX_QUEUE", 10)),
            max_links_per_message=max(1, _env_int("MAX_LINKS_PER_MESSAGE", 3)),
            proxy=_env_str("PROXY_URL") or None,
            cookies_file=_env_str("COOKIES_FILE") or None,
            log_level=_env_str("LOG_LEVEL", "INFO").upper(),
        )
        work_dir.mkdir(parents=True, exist_ok=True)
        cfg.prepare_cookies()
        return cfg

    def prepare_cookies(self) -> None:
        """Создаёт cookies.txt из COOKIES_B64, если он задан."""
        raw_b64 = _env_str("COOKIES_B64")
        if not raw_b64:
            return
        target = self.work_dir / "cookies.txt"
        try:
            data = base64.b64decode(raw_b64, validate=False)
        except (binascii.Error, ValueError):
            return
        if data:
            target.write_bytes(data)
            self.cookies_file = str(target)

    @property
    def cookies_path(self) -> str | None:
        if not self.cookies_file:
            return None
        path = Path(self.cookies_file)
        return str(path) if path.is_file() else None
