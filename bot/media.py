from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

FFMPEG = os.getenv("FFMPEG_BINARY", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_BINARY", "ffprobe")
FFMPEG_PRESET = os.getenv("FFMPEG_PRESET", "veryfast")
FFMPEG_TIMEOUT = int(os.getenv("FFMPEG_TIMEOUT_SEC", "900"))

VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".flv", ".ts"}
READY_VIDEO_CODECS = {"h264"}
READY_AUDIO_CODECS = {"aac", "mp3", "opus"}


class MediaError(Exception):
    """Ошибка обработки медиа. Сообщение показывается пользователю."""


@dataclass(slots=True)
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    video_codec: str | None
    audio_codec: str | None
    size: int


@dataclass(slots=True)
class FittedVideo:
    path: Path
    info: MediaInfo


def ensure_tools() -> None:
    missing = [name for name in (FFMPEG, FFPROBE) if shutil.which(name) is None]
    if missing:
        raise MediaError(
            "На сервере не найден ffmpeg/ffprobe. Установите ffmpeg или используйте Docker-образ."
        )


def _run(cmd: list[str], timeout: int = FFMPEG_TIMEOUT) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError("Обработка видео заняла слишком много времени. Попробуйте ссылку покороче.") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-3:]
        log.error("ffmpeg failed: %s | %s", " ".join(cmd), " / ".join(tail))
        raise MediaError("Не удалось обработать видео (ошибка ffmpeg).")
    return proc


def probe(path: Path) -> MediaInfo:
    ensure_tools()
    proc = _run(
        [
            FFPROBE,
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=120,
    )
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError("Не удалось прочитать параметры видео.") from exc

    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video is None:
        raise MediaError("В ссылке нет видео — нечего отправлять.")

    fmt = data.get("format") or {}
    duration = 0.0
    for raw in (video.get("duration"), fmt.get("duration")):
        try:
            duration = float(raw)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    width = int(video.get("width") or 0)
    height = int(video.get("height") or 0)
    # Учитываем поворот телефона (вертикальные Reels/TikTok).
    rotation = 0
    for side in video.get("side_data_list") or []:
        if "rotation" in side:
            try:
                rotation = abs(int(float(side["rotation"])))
            except (TypeError, ValueError):
                rotation = 0
    if rotation % 180 == 90:
        width, height = height, width

    return MediaInfo(
        path=path,
        duration=duration,
        width=width,
        height=height,
        video_codec=(video.get("codec_name") or "").lower() or None,
        audio_codec=(audio.get("codec_name") or "").lower() if audio else None,
        size=path.stat().st_size if path.is_file() else 0,
    )


def _telegram_ready(info: MediaInfo) -> bool:
    if info.video_codec not in READY_VIDEO_CODECS:
        return False
    if info.audio_codec is not None and info.audio_codec not in READY_AUDIO_CODECS:
        return False
    return True


def remux(src: Path, dst: Path) -> None:
    _run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(dst),
        ]
    )


def make_thumbnail(src: Path, work_dir: Path, duration: float) -> Path | None:
    """Кадр-превью для красивого сообщения в Telegram (не критично, ошибки глотаем)."""
    try:
        seek = min(max(duration * 0.25, 0.5), 5.0) if duration > 0 else 1.0
        out = work_dir / "thumb.jpg"
        _run(
            [
                FFMPEG,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-ss",
                f"{seek:.2f}",
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-vf",
                "scale=320:-2",
                "-q:v",
                "5",
                str(out),
            ],
            timeout=120,
        )
        return out if out.is_file() and out.stat().st_size > 0 else None
    except MediaError:
        return None


def _encode(src: Path, dst: Path, height: int, crf: int, preset: str) -> None:
    scale = f"scale=-2:min(ih\\,{height})"
    _run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            scale,
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-crf",
            str(crf),
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(dst),
        ]
    )


def _encode_with_bitrate(src: Path, dst: Path, height: int, video_kbps: int, preset: str) -> None:
    scale = f"scale=-2:min(ih\\,{height})"
    _run(
        [
            FFMPEG,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(src),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-vf",
            scale,
            "-c:v",
            "libx264",
            "-preset",
            preset,
            "-b:v",
            f"{video_kbps}k",
            "-maxrate",
            f"{video_kbps}k",
            "-bufsize",
            f"{video_kbps * 2}k",
            "-profile:v",
            "high",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(dst),
        ]
    )


def fit_for_telegram(src: Path, work_dir: Path, max_bytes: int) -> FittedVideo:
    """Готовит mp4, который Telegram примет и не будет сильно пережимать."""
    ensure_tools()
    src = Path(src)
    if not src.is_file() or src.stat().st_size == 0:
        raise MediaError("Скачанное видео пустое или повреждено.")

    info = probe(src)
    out = work_dir / "telegram.mp4"

    # 1. Файл уже в нужном формате — просто переставляем moov atom для стриминга.
    if _telegram_ready(info) and info.size <= max_bytes:
        remux(src, out)
        if out.is_file() and 0 < out.stat().st_size <= max_bytes:
            final = probe(out)
            log.info("remux ok: %s -> %s bytes", src.name, final.size)
            return FittedVideo(out, final)

    # 2. Перекодируем: сначала лучшее качество, затем ниже, пока не влезем в лимит.
    preset = FFMPEG_PRESET
    ladder: list[tuple[int, int]] = [(1080, 27), (720, 26), (480, 28), (360, 30)]
    if info.duration > 0:
        needed_kbps = int(max_bytes * 8 / info.duration / 1000)
        if needed_kbps < 1200:
            ladder = [step for step in ladder if step[0] <= 720]

    for height, crf in ladder:
        if info.height and height > info.height + 8 and height > 720:
            continue
        log.info("transcode %s -> %sp crf=%s preset=%s", src.name, height, crf, preset)
        _encode(src, out, height, crf, preset)
        if not out.is_file():
            continue
        size = out.stat().st_size
        log.info("transcode result: %s bytes (limit %s)", size, max_bytes)
        if 0 < size <= max_bytes:
            return FittedVideo(out, probe(out))

    # 3. Последний шанс: считаем битрейт под лимит вручную.
    duration = info.duration or 0
    if duration > 0:
        audio_kbps = 96
        total_kbps = int((max_bytes * 8 / duration / 1000) * 0.92)
        video_kbps = max(220, total_kbps - audio_kbps)
        for height in (480, 360):
            log.info("bitrate transcode -> %sp %s kbps", height, video_kbps)
            _encode_with_bitrate(src, out, height, video_kbps, preset)
            if out.is_file() and 0 < out.stat().st_size <= max_bytes:
                return FittedVideo(out, probe(out))

    raise MediaError(
        "Видео слишком большое для отправки в Telegram даже после сжатия. "
        "Попробуйте ролик покороче."
    )
