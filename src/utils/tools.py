from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

tz = ZoneInfo("Asia/Shanghai")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
BUILD_DIR = BASE_DIR / "build"


def raw_date_to_unix_timestamp(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    tz_aware_datetime = dt.replace(tzinfo=tz)
    return int(tz_aware_datetime.timestamp())
