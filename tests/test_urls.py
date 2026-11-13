from __future__ import annotations

import unittest

from bot.urls import extract_links, host_of

HOSTS = ("youtube.com", "youtu.be", "instagram.com", "tiktok.com")


class ExtractLinksTest(unittest.TestCase):
    def test_youtube_shorts(self) -> None:
        text = "смотри https://www.youtube.com/shorts/abc123?feature=share"
        self.assertEqual(
            extract_links(text, HOSTS),
            ["https://www.youtube.com/shorts/abc123?feature=share"],
        )

    def test_youtu_be(self) -> None:
        self.assertEqual(
            extract_links("https://youtu.be/abc123", HOSTS),
            ["https://youtu.be/abc123"],
        )

    def test_instagram_reel_with_punctuation(self) -> None:
        text = "вот рилс (https://www.instagram.com/reel/XYZ987/?igsh=abc)."
        self.assertEqual(
            extract_links(text, HOSTS),
            ["https://www.instagram.com/reel/XYZ987/?igsh=abc"],
        )

    def test_tiktok_short_link(self) -> None:
        self.assertEqual(
            extract_links("https://vm.tiktok.com/ZMabc123/", HOSTS),
            ["https://vm.tiktok.com/ZMabc123/"],
        )

    def test_unsupported_host_ignored(self) -> None:
        self.assertEqual(extract_links("https://example.com/video", HOSTS), [])

    def test_host_spoofing_ignored(self) -> None:
        self.assertEqual(extract_links("https://tiktok.com.evil.com/x", HOSTS), [])
        self.assertEqual(extract_links("https://notyoutube.com/x", HOSTS), [])

    def test_duplicates_removed(self) -> None:
        text = "https://youtu.be/a https://youtu.be/a"
        self.assertEqual(extract_links(text, HOSTS), ["https://youtu.be/a"])

    def test_multiple_links_order(self) -> None:
        text = "https://youtu.be/a и https://www.tiktok.com/@u/video/1"
        self.assertEqual(
            extract_links(text, HOSTS),
            ["https://youtu.be/a", "https://www.tiktok.com/@u/video/1"],
        )

    def test_host_normalization(self) -> None:
        self.assertEqual(host_of("https://m.youtube.com/shorts/1"), "youtube.com")
        self.assertEqual(host_of("https://vm.tiktok.com/abc"), "tiktok.com")


if __name__ == "__main__":
    unittest.main()
