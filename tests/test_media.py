from __future__ import annotations

import struct
import tempfile
import unittest
from pathlib import Path

from bot.media import (
    MediaInfo,
    _mvhd_duration,
    _parse_rate,
    _scaled,
    _target_height,
    _tkhd_size,
    fit_for_telegram,
    sniff_mp4,
)


def make_info(height: int) -> MediaInfo:
    return MediaInfo(
        path=Path("x.mp4"),
        duration=60,
        width=1080,
        height=height,
        fps=30,
        video_codec="h264",
        audio_codec="aac",
        size=1,
    )


def build_mp4(path: Path, *, faststart: bool, video: bytes = b"avc1", audio: bytes = b"mp4a") -> Path:
    """Минимальный набор атомов: достаточно для sniff_mp4, ffprobe не нужен."""
    ftyp = struct.pack(">I4s", 16, b"ftyp") + b"isom" + b"\x00" * 4
    payload = b"\x00" * 8 + video + b"\x00" * 4 + audio
    moov = struct.pack(">I4s", 8 + len(payload), b"moov") + payload
    mdat = struct.pack(">I4s", 16, b"mdat") + b"\x00" * 8
    parts = [ftyp, moov, mdat] if faststart else [ftyp, mdat, moov]
    path.write_bytes(b"".join(parts))
    return path


class RateTest(unittest.TestCase):
    def test_fraction(self) -> None:
        self.assertAlmostEqual(_parse_rate("30000/1001"), 29.97, places=2)

    def test_plain(self) -> None:
        self.assertEqual(_parse_rate("60"), 60.0)

    def test_bad(self) -> None:
        self.assertEqual(_parse_rate("0/0"), 0.0)
        self.assertEqual(_parse_rate(None), 0.0)


class TargetHeightTest(unittest.TestCase):
    def test_small_source_not_upscaled_much(self) -> None:
        self.assertEqual(_target_height(make_info(360), 8000), 360)

    def test_portrait_1080p_kept(self) -> None:
        self.assertEqual(_target_height(make_info(1920), 5000), 1920)

    def test_big_source_limited(self) -> None:
        self.assertEqual(_target_height(make_info(3840), 5000), 1920)
        self.assertEqual(_target_height(make_info(3840), 2000), 1280)
        self.assertEqual(_target_height(make_info(3840), 1000), 720)
        self.assertEqual(_target_height(make_info(3840), 500), 480)

    def test_unknown_bitrate(self) -> None:
        self.assertEqual(_target_height(make_info(2160), None), 1280)


class ScaledTest(unittest.TestCase):
    def test_no_upscale(self) -> None:
        self.assertEqual(_scaled(make_info(720), 1080), (1080, 720))

    def test_even_width(self) -> None:
        wide, high = _scaled(make_info(1920), 720)
        self.assertEqual(high, 720)
        self.assertEqual(wide % 2, 0)
        self.assertAlmostEqual(wide / 720, 1080 / 1920, delta=0.01)


class SniffTest(unittest.TestCase):
    def test_mvhd_duration(self) -> None:
        content = bytearray(100)
        content[0] = 0  # version 0
        struct.pack_into(">I", content, 12, 1000)  # timescale
        struct.pack_into(">I", content, 16, 39000)  # duration
        box = struct.pack(">I4s", 8 + len(content), b"mvhd") + bytes(content)
        self.assertAlmostEqual(_mvhd_duration(box, 8, len(box)), 39.0)

    def test_tkhd_size_and_rotation(self) -> None:
        content = bytearray(84)
        struct.pack_into(">I", content, 76, 720 << 16)
        struct.pack_into(">I", content, 80, 1280 << 16)
        box = struct.pack(">I4s", 8 + len(content), b"tkhd") + bytes(content)
        self.assertEqual(_tkhd_size(box, 8, len(box)), (720, 1280))

        struct.pack_into(">i", content, 76 - 32, 1 << 16)  # b != 0 -> поворот 90°
        rotated = struct.pack(">I4s", 8 + len(content), b"tkhd") + bytes(content)
        self.assertEqual(_tkhd_size(rotated, 8, len(rotated)), (1280, 720))

    def test_faststart_h264_aac(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = build_mp4(Path(tmp) / "a.mp4", faststart=True)
            sniff = sniff_mp4(path)
            self.assertIsNotNone(sniff)
            assert sniff is not None
            self.assertTrue(sniff.faststart)
            self.assertEqual(sniff.video_codec, "h264")
            self.assertEqual(sniff.audio_codec, "aac")

    def test_moov_at_end(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = build_mp4(Path(tmp) / "b.mp4", faststart=False)
            sniff = sniff_mp4(path)
            assert sniff is not None
            self.assertFalse(sniff.faststart)

    def test_hevc_detected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = build_mp4(Path(tmp) / "c.mp4", faststart=True, video=b"hvc1")
            sniff = sniff_mp4(path)
            assert sniff is not None
            self.assertEqual(sniff.video_codec, "hevc")

    def test_not_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.webm"
            path.write_bytes(b"\x1aE\xdf\xa3" + b"\x00" * 64)
            self.assertIsNone(sniff_mp4(path))

    def test_fast_path_sends_as_is(self) -> None:
        """Готовый H.264/AAC с moov впереди не должен трогать ffmpeg."""
        with tempfile.TemporaryDirectory() as tmp:
            src = build_mp4(Path(tmp) / "e.mp4", faststart=True)
            hint = MediaInfo(
                path=src,
                duration=39.0,
                width=1080,
                height=1920,
                fps=60,
                video_codec="h264",
                audio_codec="aac",
                size=src.stat().st_size,
            )
            fitted = fit_for_telegram(src, Path(tmp), 48 * 1024 * 1024, hint)
            self.assertEqual(fitted.path, src)
            self.assertEqual(fitted.info.duration, 39.0)
            self.assertEqual(fitted.info.height, 1920)


if __name__ == "__main__":
    unittest.main()
