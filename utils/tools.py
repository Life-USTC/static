from json import dump
from pydantic.json import pydantic_encoder
from datetime import datetime
from pytz import timezone
from typing import Any

tz = timezone("Asia/Shanghai")


def raw_date_to_unix_timestamp(date_str: str) -> int:
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    tz_aware_datetime = tz.localize(dt)
    return int(tz_aware_datetime.timestamp())


def save_json(obj: Any, path: str) -> None:
    with open(path, "w") as f:
        # encoded = jsonpickle.encode(obj, indent=4)
        # if isinstance(obj, BaseModel):
        # encoded = obj.model_dump_json(indent=4)
        # else:
        #     encoded = jsonpickle.encode(obj, indent=4)
        # if encoded is not None:
        #     f.write(encoded)
        dump(obj, f, indent=4, default=pydantic_encoder)
