import datetime
import logging
from pathlib import Path
from typing import cast

import feedgenerator
import feedparser
import html2text
import httpx
import yaml
from tqdm import tqdm

from .utils.tj_rss import tj_ustc_RSS
from .utils.tools import BUILD_DIR, RSS_CONFIG_PATH

logger = logging.getLogger(__name__)


async def get_and_clean_feed(url: str, path_to_save: Path):
    try:
        response = httpx.get(url, timeout=60, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as error:
        logger.warning(
            "Keeping cached %s after feed fetch failed: %s", path_to_save, error
        )
        return

    feed = feedparser.parse(response.content)

    if feed.bozo:
        logger.warning("Feed %s reported a parse warning: %s", url, feed.bozo_exception)

    if not feed.entries:
        logger.warning(
            "Keeping cached %s because the fetched feed has no entries%s",
            path_to_save,
            f": {feed.bozo_exception}" if feed.bozo else "",
        )
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

    written_items = 0
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
            written_items += 1
        except Exception as e:
            logger.exception("Failed to process feed entry: %s", e)

    if written_items == 0:
        logger.warning(
            "Keeping cached %s because no fetched entries could be parsed",
            path_to_save,
        )
        return

    with open(path_to_save, "w", encoding="utf-8") as f:
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
