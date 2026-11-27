from __future__ import annotations


def build_health(builders: dict[str, dict[str, str]]) -> str:
    return (
        "complete"
        if builders and all(row.get("status") == "ok" for row in builders.values())
        else "degraded"
    )
