from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from pytz import timezone

tz = timezone("Asia/Shanghai")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
BUILD_DIR = BASE_DIR / "build"
STATIC_DIR = BASE_DIR / "static"
RSS_CONFIG_PATH = BASE_DIR / "rss-config.yaml"


def raw_date_to_unix_timestamp(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    tz_aware_datetime = tz.localize(dt)
    return int(tz_aware_datetime.timestamp())


def compose_start_end(date_str: str, start_hhmm: int, end_hhmm: int) -> tuple[int, int]:
    def compose_datetime(date_str: str, hhmm: int) -> int:
        base = raw_date_to_unix_timestamp(date_str)
        return base + int(hhmm // 100) * 3600 + int(hhmm % 100) * 60

    return compose_datetime(date_str, start_hhmm), compose_datetime(date_str, end_hhmm)


def join_nonempty(values: Iterable[str], sep: str = ", ") -> str:
    return sep.join([v for v in values if v])
