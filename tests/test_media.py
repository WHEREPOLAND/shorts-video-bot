from __future__ import annotations

import unittest

from bot.media import MediaInfo, _parse_rate, _target_height


def make_info(height: int) -> MediaInfo:
    return MediaInfo(
        path=__import__("pathlib").Path("x.mp4"),
        duration=60,
        width=1080,
        height=height,
        fps=30,
        video_codec="h264",
        audio_codec="aac",
        size=1,
    )


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


if __name__ == "__main__":
    unittest.main()
