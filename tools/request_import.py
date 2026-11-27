from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from urllib.request import Request, urlopen


def signature(secret: str, timestamp: str, delivery: str, body: bytes) -> str:
    return hmac.new(
        secret.encode(), f"{timestamp}.{delivery}.".encode() + body, hashlib.sha256
    ).hexdigest()


def read_json(url: str, *, maximum_bytes: int = 65_536) -> dict:
    with urlopen(url, timeout=30) as response:
        body = response.read(maximum_bytes + 1)
    if len(body) > maximum_bytes:
        raise ValueError("JSON response is too large")
    value = json.loads(body)
    if not isinstance(value, dict):
        raise ValueError("JSON response must be an object")
    return value


def main() -> None:
    manifest_url = os.environ.get(
        "STATIC_SNAPSHOT_MANIFEST_URL",
        "https://static.life-ustc.tiankaima.dev/snapshot-manifest.json",
    )
    webhook_url = os.environ["STATIC_IMPORT_WEBHOOK_URL"]
    secret = os.environ["STATIC_IMPORT_WEBHOOK_SECRET"]
    manifest = read_json(manifest_url)
    delivery = uuid.uuid4().hex
    timestamp = str(int(time.time()))
    body = json.dumps(
        {
            "manifestUrl": manifest_url,
            "sha256": manifest["snapshot"]["sha256"],
            "generatedAt": manifest["generatedAt"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    request = Request(
        webhook_url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Static-Import-Timestamp": timestamp,
            "X-Static-Import-Delivery": delivery,
            "X-Static-Import-Signature": signature(secret, timestamp, delivery, body),
        },
    )
    with urlopen(request, timeout=30) as response:
        result = json.load(response)
    if result.get("deliveryId") != delivery:
        raise RuntimeError("webhook returned a different delivery ID")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
