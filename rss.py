import feedparser
import html2text
import yaml
import asyncio
from pathlib import Path
from tqdm import tqdm
import feedgenerator
import datetime
from typing import cast

from utils.tj_rss import tj_ustc_RSS


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
                # Tue, 10 Aug 2021 00:00:00 GMT
                date = datetime.datetime.strptime(date_raw, "%a, %d %b %Y %H:%M:%S %Z")
            except ValueError:
                # Tue, 10 Aug 2021 00:00:00 +0800
                date = datetime.datetime.strptime(date_raw, "%a, %d %b %Y %H:%M:%S %z")

            description = handler.handle(str(getattr(entry, "description", "")))

            new_feed.add_item(
                title=entry.title,
                link=entry.link,
                description=description,
                pubdate=date,
            )
        except Exception as e:
            print(e)

    with open(path_to_save, "w") as f:
        new_feed.write(f, "utf-8")


async def make_rss():
    # load ./rss-config.yaml
    config_path = Path(__file__).resolve().parent / "rss-config.yaml"
    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    base_path = Path(__file__).resolve().parent
    rss_path = base_path / "build" / "rss"
    rss_path.mkdir(parents=True, exist_ok=True)

    tj_ustc_RSS(rss_path)

    for feed in tqdm(config["feeds"], position=1, leave=True, desc="Processing feeds"):
        filepath = rss_path / feed["xmlFilename"]
        await get_and_clean_feed(feed["url"], filepath)


def main():
    asyncio.run(make_rss())


if __name__ == "__main__":
    main()
