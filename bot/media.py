from __future__ import annotations

import json
import logging
import os
import shutil
import struct
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path

log = logging.getLogger(__name__)

FFMPEG = os.getenv("FFMPEG_BINARY", "ffmpeg")
FFPROBE = os.getenv("FFPROBE_BINARY", "ffprobe")
FFMPEG_PRESET = os.getenv("FFMPEG_PRESET", "ultrafast")
FFMPEG_TIMEOUT = int(os.getenv("FFMPEG_TIMEOUT_SEC", "900"))

READY_VIDEO_CODECS = {"h264"}
READY_AUDIO_CODECS = {"aac", "mp3"}

# Быстрое чтение MP4-атомов: если moov впереди mdat, файл уже пригоден для стриминга
# и его можно отправить в Telegram без единого вызова ffmpeg.
_MP4_TOP_ATOMS = {
    b"ftyp",
    b"moov",
    b"mdat",
    b"free",
    b"skip",
    b"wide",
    b"pnot",
    b"uuid",
    b"styp",
    b"sidx",
    b"moof",
    b"meta",
}
_VIDEO_FOURCC = {
    b"avc1": "h264",
    b"avc3": "h264",
    b"hvc1": "hevc",
    b"hev1": "hevc",
    b"av01": "av1",
    b"vp09": "vp9",
    b"mp4v": "mpeg4",
}
_AUDIO_FOURCC = {
    b"mp4a": "aac",
    b"opus": "opus",
    b"ac-3": "ac3",
    b"ec-3": "eac3",
    b"alac": "alac",
    b"mp3 ": "mp3",
    b".mp3": "mp3",
}
_MOOV_SCAN_LIMIT = 8 * 1024 * 1024


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

    def moved_to(self, path: Path) -> "MediaInfo":
        return replace(self, path=path, size=path.stat().st_size)


@dataclass(slots=True)
class Sniff:
    faststart: bool
    video_codec: str | None
    audio_codec: str | None
    duration: float = 0.0
    width: int = 0
    height: int = 0


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


def _iter_boxes(payload: bytes, start: int = 0, end: int | None = None):
    end = len(payload) if end is None else end
    offset = start
    while offset + 8 <= end:
        size, kind = struct.unpack(">I4s", payload[offset : offset + 8])
        header = 8
        if size == 1:
            if offset + 16 > end:
                break
            size = struct.unpack(">Q", payload[offset + 8 : offset + 16])[0]
            header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            break
        yield kind, offset + header, offset + size
        offset += size


def _mvhd_duration(payload: bytes, start: int, end: int) -> float:
    data = payload[start:end]
    if len(data) < 20:
        return 0.0
    try:
        if data[0] == 1:
            timescale = struct.unpack(">I", data[20:24])[0]
            duration = struct.unpack(">Q", data[24:32])[0]
        else:
            timescale = struct.unpack(">I", data[12:16])[0]
            duration = struct.unpack(">I", data[16:20])[0]
    except struct.error:
        return 0.0
    return duration / timescale if timescale else 0.0


def _tkhd_size(payload: bytes, start: int, end: int) -> tuple[int, int]:
    data = payload[start:end]
    if not data:
        return 0, 0
    base = 84 if data[0] == 1 else 76
    if len(data) < base + 8:
        return 0, 0
    width, height = struct.unpack(">II", data[base : base + 8])
    # Ненулевые b/c в матрице означают поворот на 90°/270° — меняем стороны местами.
    matrix_b = struct.unpack(">i", data[base - 32 : base - 28])[0]
    matrix_c = struct.unpack(">i", data[base - 24 : base - 20])[0]
    if matrix_b or matrix_c:
        width, height = height, width
    return width >> 16, height >> 16


def _moov_meta(payload: bytes) -> tuple[float, int, int]:
    duration = 0.0
    width = height = 0
    for kind, start, end in _iter_boxes(payload):
        if kind == b"mvhd":
            duration = _mvhd_duration(payload, start, end)
        elif kind == b"trak":
            for child, cstart, cend in _iter_boxes(payload, start, end):
                if child == b"tkhd":
                    track_w, track_h = _tkhd_size(payload, cstart, cend)
                    if track_w > width:
                        width, height = track_w, track_h
    return duration, width, height


def sniff_mp4(path: Path) -> Sniff | None:
    """Читает атомы MP4 без ffprobe: кодеки, длительность, размеры и moov в начале."""
    try:
        total = path.stat().st_size
        moov: tuple[int, int] | None = None
        mdat_offset: int | None = None
        first: bytes | None = None
        with path.open("rb") as handle:
            offset = 0
            while offset < total:
                handle.seek(offset)
                head = handle.read(8)
                if len(head) < 8:
                    break
                size, kind = struct.unpack(">I4s", head)
                if first is None:
                    first = kind
                header_len = 8
                if size == 1:
                    extra = handle.read(8)
                    if len(extra) < 8:
                        break
                    size = struct.unpack(">Q", extra)[0]
                    header_len = 16
                elif size == 0:
                    size = total - offset
                if size < header_len:
                    break
                if kind == b"moov" and moov is None:
                    moov = (offset + header_len, size - header_len)
                elif kind == b"mdat" and mdat_offset is None:
                    mdat_offset = offset
                offset += size
        if first not in _MP4_TOP_ATOMS or moov is None:
            return None

        limit = min(moov[1], _MOOV_SCAN_LIMIT)
        with path.open("rb") as handle:
            handle.seek(moov[0])
            payload = handle.read(limit)
        video = next((name for fourcc, name in _VIDEO_FOURCC.items() if fourcc in payload), None)
        audio = next((name for fourcc, name in _AUDIO_FOURCC.items() if fourcc in payload), None)
        duration, width, height = _moov_meta(payload)
        return Sniff(
            faststart=mdat_offset is None or moov[0] < mdat_offset,
            video_codec=video,
            audio_codec=audio,
            duration=duration,
            width=width,
            height=height,
        )
    except (OSError, struct.error):
        return None


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
        audio_codec=(video.get("codec_name") or "").lower() if audio else None,
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

    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-profile:v",
        "high",
        "-pix_fmt",
        "yuv420p",
        "-threads",
        "0",
    ]
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


def _scaled(info: MediaInfo, target_height: int) -> tuple[int, int]:
    """Размеры кадра после `scale=-2:min(ih,H)` без второго ffprobe."""
    src_h = info.height or 0
    src_w = info.width or 0
    if src_h <= 0 or src_w <= 0:
        return 0, target_height
    if src_h <= target_height:
        return src_w, src_h
    width = int(round(src_w * target_height / src_h))
    width -= width % 2
    return max(2, width), target_height


def fit_for_telegram(
    src: Path,
    work_dir: Path,
    max_bytes: int,
    hint: MediaInfo | None = None,
) -> FittedVideo:
    """Готовит mp4, который Telegram примет и не будет сильно пережимать.

    Быстрый путь (обычный случай) вообще не запускает ffmpeg: файлы TikTok и Reels
    уже лежат в H.264/AAC с moov в начале, поэтому отправляются как есть.
    """
    src = Path(src)
    if not src.is_file() or src.stat().st_size == 0:
        raise MediaError("Скачанное видео пустое или повреждено.")

    size = src.stat().st_size
    out = work_dir / "telegram.mp4"
    sniff = sniff_mp4(src)

    if (
        sniff is not None
        and sniff.faststart
        and sniff.video_codec in READY_VIDEO_CODECS
        and (sniff.audio_codec is None or sniff.audio_codec in READY_AUDIO_CODECS)
        and size <= max_bytes
    ):
        info = hint.moved_to(src) if hint is not None else probe(src)
        info.video_codec = sniff.video_codec
        if sniff.audio_codec:
            info.audio_codec = sniff.audio_codec
        if sniff.duration > 0:
            info.duration = sniff.duration
        if sniff.width > 0 and sniff.height > 0:
            info.width, info.height = sniff.width, sniff.height
        log.info(
            "no processing needed: %s (%.1f MB, %s/%s)",
            src.name,
            size / 1024 / 1024,
            sniff.video_codec,
            sniff.audio_codec or "no audio",
        )
        return FittedVideo(src, info)

    ensure_tools()
    info = probe(src)

    # 1. Файл уже H.264/AAC — только переставляем moov atom для стриминга.
    if _telegram_ready(info) and info.size <= max_bytes:
        try:
            remux(src, out)
            if out.is_file() and 0 < out.stat().st_size <= max_bytes:
                final = info.moved_to(out)
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
            width, height = _scaled(info, target_height)
            final = MediaInfo(
                path=out,
                duration=info.duration,
                width=width,
                height=height,
                fps=min(info.fps, 30.0) if info.fps > 31 else info.fps,
                video_codec="h264",
                audio_codec="aac",
                size=size,
            )
            return FittedVideo(out, final)

    raise MediaError(
        "Видео слишком большое для отправки в Telegram даже после сжатия. "
        "Попробуйте ролик покороче."
    )
