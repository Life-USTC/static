import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from src.rss import get_and_clean_feed


class RssCacheSafetyTest(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_failure_keeps_cached_feed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_dir:
            feed_path = Path(temporary_dir) / "feed.xml"
            feed_path.write_text("cached", encoding="utf-8")
            with patch(
                "src.rss.httpx.get",
                side_effect=httpx.ConnectError("offline"),
            ):
                await get_and_clean_feed("https://example.com/feed", feed_path)

            self.assertEqual(feed_path.read_text(encoding="utf-8"), "cached")

    async def test_unparseable_entries_keep_cached_feed(self) -> None:
        response = MagicMock()
        response.content = b"<rss />"
        parsed_feed = MagicMock()
        parsed_feed.entries = [
            {
                "title": "Entry",
                "link": "https://example.com/entry",
                "published": "unsupported date",
                "description": "Description",
            }
        ]
        parsed_feed.feed = {"title": "Feed"}

        with tempfile.TemporaryDirectory() as temporary_dir:
            feed_path = Path(temporary_dir) / "feed.xml"
            feed_path.write_text("cached", encoding="utf-8")
            with (
                patch("src.rss.httpx.get", return_value=response),
                patch("src.rss.feedparser.parse", return_value=parsed_feed),
            ):
                await get_and_clean_feed("https://example.com/feed", feed_path)

            self.assertEqual(feed_path.read_text(encoding="utf-8"), "cached")
