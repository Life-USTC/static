from datetime import datetime
from json import dump
from pathlib import Path
from pydantic.json import pydantic_encoder
from pytz import timezone
from typing import Any, Iterable, Tuple
from urllib.parse import urlparse

tz = timezone("Asia/Shanghai")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
BUILD_DIR = BASE_DIR / "build"
STATIC_DIR = BASE_DIR / "static"
RSS_CONFIG_PATH = BASE_DIR / "rss-config.yaml"


def raw_date_to_unix_timestamp(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    tz_aware_datetime = tz.localize(dt)
    return int(tz_aware_datetime.timestamp())


def save_json(obj: Any, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or path.exists():
        path.unlink()

    with open(path, "w") as f:
        dump(obj, f, default=pydantic_encoder, ensure_ascii=False)


def compose_start_end(date_str: str, start_hhmm: int, end_hhmm: int) -> Tuple[int, int]:
    def compose_datetime(date_str: str, hhmm: int) -> int:
        base = raw_date_to_unix_timestamp(date_str)
        return base + int(hhmm // 100) * 3600 + int(hhmm % 100) * 60

    return compose_datetime(date_str, start_hhmm), compose_datetime(date_str, end_hhmm)


def cache_dir_from_url(url: str) -> Path:
    parsed = urlparse(url)

    host = parsed.netloc
    path = parsed.path.lstrip("/")  # strip leading '/'

    host_abbrs = {
        "catalog.ustc.edu.cn": "catalog",
        "jw.ustc.edu.cn": "jw",
    }

    if host in host_abbrs.keys():
        host = host_abbrs[host]

    return Path("build") / "cache" / host / path


def join_nonempty(values: Iterable[str], sep: str = ", ") -> str:
    return sep.join([v for v in values if v])
