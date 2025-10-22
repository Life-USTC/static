import feedparser
import html2text
import yaml
import os
import asyncio
from tqdm import tqdm
import feedgenerator
import datetime
from typing import cast

from utils.tj_rss import tj_ustc_RSS


async def get_and_clean_feed(url: str, path_to_save: str):
    feed = feedparser.parse(url)

    if not feed.entries:
        return

    feed_title = getattr(feed.feed, "title", "RSS Feed")
    new_feed = feedgenerator.Rss201rev2Feed(
        title=cast(str, feed_title),
        link=f"https://static.life-ustc.tiankaima.dev/rss/{path_to_save.split('/')[-1]}",
        description="",
    )

    handler = html2text.HTML2Text()
    handler.ignore_links = True
    handler.ignore_images = True

    for entry in tqdm(
        feed.entries,
        position=0,
        leave=True,
        desc=f"Processing {path_to_save.split('/')[-1]}",
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
    with open("rss-config.yaml", "r") as f:
        config = yaml.safe_load(f)

    base_path = os.path.dirname(os.path.abspath(__file__))
    rss_path = os.path.join(base_path, "build", "rss")

    if not os.path.exists(rss_path):
        os.mkdir(rss_path)

    tj_ustc_RSS(rss_path)

    for feed in tqdm(config["feeds"], position=1, leave=True, desc="Processing feeds"):
        filepath = os.path.join(rss_path, feed["xmlFilename"])
        await get_and_clean_feed(feed["url"], filepath)


def main():
    asyncio.run(make_rss())


if __name__ == "__main__":
    main()
