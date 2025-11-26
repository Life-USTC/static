"""
Make RSS feed from: http://www.tj.ustc.edu.cn/tzgg/list.htm

Save rss to outputDir/tj_ustc.xml
"""

import datetime
from pathlib import Path
from typing import cast

import requests
from bs4 import BeautifulSoup, Tag
from feedgen.feed import FeedGenerator
from requests.adapters import HTTPAdapter

url = "http://www.tj.ustc.edu.cn/tzgg/list.htm"
title = "体育教学中心"
description = "体育教学中心，http://www.tj.ustc.edu.cn"

headers = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36 Edg/113.0.1774.57",
}


def parseHTML(html: str):
    soup = BeautifulSoup(html, "html.parser")
    div = soup.find("div", {"id": "wp_news_w5"})
    if not div or not isinstance(div, Tag):
        return []

    ul = div.find("ul")
    if not ul or not isinstance(ul, Tag):
        return []

    items = ul.find_all("li", {"class": "item"})
    result = []
    for item in items:
        if not isinstance(item, Tag):
            continue

        time_tag = item.find("time")
        if not time_tag or not isinstance(time_tag, Tag):
            continue
        date_raw = time_tag.text

        date = datetime.datetime.strptime(date_raw, "%Y-%m-%d").astimezone(
            tz=datetime.timezone(datetime.timedelta(hours=8))
        )

        a_tag = item.find("a")
        if not a_tag or not isinstance(a_tag, Tag):
            continue

        result.append(
            {
                "title": a_tag.text,
                "link": "http://www.tj.ustc.edu.cn" + cast(str, a_tag.get("href", "")),
                "date": date,
            }
        )
    return result


def tj_ustc_RSS(output_dir: Path | str):
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    s = requests.Session()
    s.mount("http://", HTTPAdapter(max_retries=3))
    r = s.get(url, headers=headers)
    r.encoding = "utf-8"
    html = r.text

    items = parseHTML(html)

    fg = FeedGenerator()
    fg.title(title)
    fg.description(description)
    fg.link(href="http://www.tj.ustc.edu.cn/tzgg/list.htm", rel="alternate")
    fg.language("zh-CN")
    fg.lastBuildDate(datetime.datetime.utcnow().astimezone(tz=datetime.timezone.utc))
    fg.ttl(5)

    for item in items:
        fe = fg.add_entry()
        fe.title(item["title"])
        fe.link(href=item["link"])
        fe.pubDate(item["date"])

    fg.rss_file(str(output_path / "tj_ustc.xml"))
