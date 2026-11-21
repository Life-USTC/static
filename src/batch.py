"""Bounded, cancellation-safe batches and staggered resource refreshes."""

import asyncio
import hashlib
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime


async def fetch_in_batches[T, R](
    fetch: Callable[[T], Awaitable[R]],
    items: Iterable[T],
    concurrency: int,
    interval: float,
) -> list[R]:
    if concurrency < 1 or interval < 0:
        raise ValueError("Concurrency must be positive and interval nonnegative")
    iterator = enumerate(items)
    results: dict[int, R] = {}
    lock = asyncio.Lock()
    next_start = 0.0
    loop = asyncio.get_running_loop()

    async def worker():
        nonlocal next_start
        for index, item in iterator:
            if interval:
                async with lock:
                    await asyncio.sleep(max(0, next_start - loop.time()))
                    next_start = loop.time() + interval / concurrency
            results[index] = await fetch(item)

    tasks = [asyncio.create_task(worker()) for _ in range(concurrency)]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return [results[index] for index in sorted(results)]


def refresh_due(key: str, timestamp: str | None, now: datetime) -> bool:
    """Recheck each resource once per fortnight without a cold-build spike."""
    if not timestamp:
        return True
    try:
        fetched = datetime.fromisoformat(timestamp)
        tomorrow = fetched.date().toordinal() + 1
        slot = int.from_bytes(hashlib.sha256(key.encode()).digest()[:4]) % 14
        return (
            fetched > now or now.date().toordinal() >= tomorrow + (slot - tomorrow) % 14
        )
    except (TypeError, ValueError):
        return True
