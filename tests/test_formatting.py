from __future__ import annotations

import unittest

from bot.formatting import build_caption, format_duration


class DurationTest(unittest.TestCase):
    def test_short(self) -> None:
        self.assertEqual(format_duration(59), "0:59")

    def test_minutes(self) -> None:
        self.assertEqual(format_duration(125), "2:05")

    def test_hours(self) -> None:
        self.assertEqual(format_duration(3725), "1:02:05")


class CaptionTest(unittest.TestCase):
    def test_html_escaped(self) -> None:
        caption = build_caption("<b>hack</b> & co", "user", "https://youtu.be/a?x=1&y=2", 61, 1080)
        self.assertIn("&lt;b&gt;hack&lt;/b&gt; &amp; co", caption)
        self.assertIn("1:01", caption)
        self.assertIn("1080p", caption)
        self.assertIn("https://youtu.be/a?x=1&amp;y=2", caption)

    def test_long_title_truncated(self) -> None:
        caption = build_caption("x" * 500, "", "https://youtu.be/a")
        self.assertLessEqual(len(caption), 1000)

    def test_empty_title(self) -> None:
        self.assertIn("Видео", build_caption("", "", "https://youtu.be/a"))


if __name__ == "__main__":
    unittest.main()
