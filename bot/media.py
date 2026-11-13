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

READY_VIDEO_CODECS = {"h264"}
READY_AUDIO_CODECS = {"aac", "mp3"}


class MediaError(Exception):
    """Ошибка обработки медиа. Сообщение показывается пользователю."""


@dataclass(slots=True)
class MediaInfo:
    path: Path
    duration: float
    width: int
    height: int
    fps: float
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


def _parse_rate(raw: str | None) -> float:
    if not raw:
        return 0.0
    try:
        if "/" in raw:
            num, den = raw.split("/", 1)
            return float(num) / float(den) if float(den) else 0.0
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


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
        fps=_parse_rate(video.get("avg_frame_rate")) or _parse_rate(video.get("r_frame_rate")),
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


def _transcode(
    src: Path,
    dst: Path,
    height: int,
    preset: str,
    fps: float,
    *,
    crf: int | None = None,
    video_kbps: int | None = None,
) -> None:
    if crf is None and video_kbps is None:
        raise ValueError("нужен crf или video_kbps")

    cmd = [
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
        f"scale=-2:min(ih\\,{height})",
    ]
    # 60 fps укорачивает вдвое только время кодирования, но не качество картинки.
    if fps > 31:
        cmd += ["-r", "30"]

    cmd += ["-c:v", "libx264", "-preset", preset, "-profile:v", "high", "-pix_fmt", "yuv420p"]
    if video_kbps is not None:
        cmd += [
            "-b:v",
            f"{video_kbps}k",
            "-maxrate",
            f"{video_kbps}k",
            "-bufsize",
            f"{video_kbps * 2}k",
        ]
        audio_kbps = 96
    else:
        cmd += ["-crf", str(crf)]
        audio_kbps = 128

    cmd += [
        "-c:a",
        "aac",
        "-b:a",
        f"{audio_kbps}k",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(dst),
    ]
    _run(cmd)


def _target_height(info: MediaInfo, total_kbps: int | None) -> int:
    source = info.height or 720
    if total_kbps is None:
        cap = 1280
    elif total_kbps < 700:
        cap = 480
    elif total_kbps < 1600:
        cap = 720
    elif total_kbps < 3200:
        cap = 1280
    else:
        cap = 1920
    return max(360, min(source, cap))


def fit_for_telegram(src: Path, work_dir: Path, max_bytes: int) -> FittedVideo:
    """Готовит mp4, который Telegram примет и не будет сильно пережимать."""
    ensure_tools()
    src = Path(src)
    if not src.is_file() or src.stat().st_size == 0:
        raise MediaError("Скачанное видео пустое или повреждено.")

    info = probe(src)
    out = work_dir / "telegram.mp4"

    # 1. Файл уже H.264/AAC — просто переставляем moov atom для стриминга.
    if _telegram_ready(info) and info.size <= max_bytes:
        try:
            remux(src, out)
            if out.is_file() and 0 < out.stat().st_size <= max_bytes:
                final = probe(out)
                log.info("remux ok: %s -> %s bytes", src.name, final.size)
                return FittedVideo(out, final)
        except MediaError:
            log.warning("remux failed for %s, falling back to transcode", src.name)

    # 2. Один проход с целевым битрейтом: сразу попадаем в лимит, без «лестницы».
    duration = info.duration or 0
    total_kbps = None
    if duration > 0:
        total_kbps = int(max_bytes * 8 / duration / 1000 * 0.92)

    height = _target_height(info, total_kbps)
    attempts: list[tuple[int, int | None]] = []
    if total_kbps:
        attempts.append((height, max(280, total_kbps - 96)))
        attempts.append((max(360, (height * 3 // 4) // 2 * 2), max(240, int((total_kbps - 96) * 0.75))))
    else:
        attempts.append((height, None))
        attempts.append((max(360, height // 2), None))

    for target_height, kbps in attempts:
        log.info(
            "transcode %s -> %sp %s preset=%s",
            src.name,
            target_height,
            f"{kbps}kbps" if kbps else "crf=27",
            FFMPEG_PRESET,
        )
        _transcode(
            src,
            out,
            target_height,
            FFMPEG_PRESET,
            info.fps,
            crf=None if kbps else 27,
            video_kbps=kbps,
        )
        if not out.is_file():
            continue
        size = out.stat().st_size
        log.info("transcode result: %s bytes (limit %s)", size, max_bytes)
        if 0 < size <= max_bytes:
            return FittedVideo(out, probe(out))

    raise MediaError(
        "Видео слишком большое для отправки в Telegram даже после сжатия. "
        "Попробуйте ролик покороче."
    )
