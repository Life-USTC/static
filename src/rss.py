import datetime
import logging
from pathlib import Path
from typing import cast

import feedgenerator
import feedparser
import html2text
import yaml
from tqdm import tqdm

from .utils.tj_rss import tj_ustc_RSS
from .utils.tools import BUILD_DIR, RSS_CONFIG_PATH


logger = logging.getLogger(__name__)


async def get_and_clean_feed(url: str, path_to_save: Path):
    feed = feedparser.parse(url)

    if not feed.entries:
        return

    feed_title = getattr(feed.feed, "title", "RSS Feed")
    filename = path_to_save.name
    new_feed = feedgenerator.Rss201rev2Feed(
        title=cast(str, feed_title),
        link=f"https://static.life-ustc.tiankaima.dev/rss/{filename}",
        description="",
    )

    handler = html2text.HTML2Text()
    handler.ignore_links = True
    handler.ignore_images = True

    for entry in tqdm(
        feed.entries,
        position=0,
        leave=True,
        desc=f"Processing {filename}",
    ):
        try:
            date_raw = str(getattr(entry, "published", ""))
            try:
                date = datetime.datetime.strptime(date_raw, "%a, %d %b %Y %H:%M:%S %z")
            except ValueError:
                date = datetime.datetime.strptime(date_raw, "%a, %d %b %Y %H:%M:%S %Z")

            description = handler.handle(str(getattr(entry, "description", "")))

            new_feed.add_item(
                title=entry.title,
                link=entry.link,
                description=description,
                pubdate=date,
            )
        except Exception as e:
            logger.exception("Failed to process feed entry: %s", e)

    with open(path_to_save, "w") as f:
        new_feed.write(f, "utf-8")


async def make_rss() -> None:
    with open(RSS_CONFIG_PATH) as f:
        config = yaml.safe_load(f)

    rss_path = BUILD_DIR / "rss"
    rss_path.mkdir(parents=True, exist_ok=True)

    tj_ustc_RSS(rss_path)

    for feed in tqdm(config["feeds"], position=1, leave=True, desc="Processing feeds"):
        filepath = rss_path / feed["xmlFilename"]
        await get_and_clean_feed(feed["url"], filepath)
