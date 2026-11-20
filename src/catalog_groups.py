"""Fetch the exact classification sources used by the public course browser."""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from itertools import batched
from typing import NamedTuple
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .sqlite_store import SQLiteModelStore
from .utils.auth import RequestSession

CATALOG_GROUP_ENTRIES = "catalog_teach_course_group_entries"
CATALOG_GROUP_ENDPOINTS = "catalog_teach_course_group_endpoints"
CATALOG_GROUPS = "catalog_teach_course_groups"
CATALOG_GROUP_MEMBERS = "catalog_teach_course_group_members"

_COURSE_URL = "https://catalog.ustc.edu.cn/api/teach/course"
_CATALOG_URL = "https://catalog.ustc.edu.cn/catalog"
_MAX_CONCURRENT_REQUESTS = 3
_BATCH_INTERVAL_SECONDS = 0.15


class _CatalogEntry(NamedTuple):
    route_key: str
    source_kind: str
    source_ids: tuple[str, ...]
    name_cn: str
    name_en: str


# Parse only the browser's static route table and endpoint dispatch. Do not
# execute JavaScript or use the broader college tree as a substitute registry.
_IDENTIFIER = r"[A-Za-z_$][\w$]*"
_KIND_CASE = (
    rf"""case\s*["'](generic|quality|school)["']\s*:\s*"""
    rf"""({_IDENTIFIER})\s*=\s*["'](public|quality|department)["']\s*;"""
    r"\s*break\s*;?"
)
_DISPATCH = re.compile(
    rf"switch\s*\(\s*{_IDENTIFIER}\s*\)\s*\{{"
    rf"(?P<cases>(?:\s*(?:{_KIND_CASE})){{3}})"
)
_DISPATCH_CASE = re.compile(_KIND_CASE)
_ROUTE_LOOKUP = re.compile(
    rf"(?P<table>{_IDENTIFIER})\.find\(\s*\(?\s*(?P<entry>{_IDENTIFIER})\s*\)?\s*"
    rf"""=>\s*(?P=entry)\.code\s*===\s*{_IDENTIFIER}\s*\)\.id\.split\(["']\+["']\)"""
)
_JS_LITERAL_TOKEN = re.compile(
    rf'\s*(?:"(?:[^"\\]|\\.)*"|({_IDENTIFIER})(?=\s*:)|[{{}}\[\]:,])'
)
_SOURCE_KINDS: dict[str, str] = {
    "generic": "public",
    "school": "department",
    "quality": "quality",
}


def _browser_origin(url: str) -> bool:
    actual = urlsplit(url)
    return (actual.scheme, actual.netloc) == ("https", "catalog.ustc.edu.cn")


def _literal_array(script: str, start: int) -> list:
    tokens, depth = [], 0
    end = start + 65_536  # The official registry is about 3 KiB.
    while match := _JS_LITERAL_TOKEN.match(script, start, end):
        token = match[0].strip()
        tokens.append(json.dumps(match[1]) if match[1] is not None else token)
        start = match.end()
        if token == "[":
            depth += 1
        elif token == "]":
            depth -= 1
            if depth == 0:
                if not re.match(r"\s*[,;}]", script[start : start + 32]):
                    break
                return json.loads("".join(tokens))
    raise ValueError("The school catalog route table is no longer a static literal")


def _live_entries(script: str) -> tuple[_CatalogEntry, ...]:
    try:
        (dispatch,) = _DISPATCH.finditer(script)
        cases = _DISPATCH_CASE.findall(dispatch["cases"])
        (endpoint_kind,) = {variable for _, variable, _ in cases}
    except ValueError as error:
        raise ValueError("Cannot verify catalog browser endpoint dispatch") from error
    if {kind: endpoint for kind, _, endpoint in cases} != _SOURCE_KINDS:
        raise ValueError("Unexpected school catalog endpoint kind or duplicate case")
    selection = script[dispatch.end() : dispatch.end() + 2_500]
    routes = [match["table"] for match in _ROUTE_LOOKUP.finditer(selection)]
    endpoint_kind = re.escape(endpoint_kind)
    stem = rf"/api/teach/course/\$\{{{endpoint_kind}\}}"
    if (
        len(routes) != 2
        or routes[0] != routes[1]
        or not re.search(stem + rf"/\$\{{{_IDENTIFIER}\}}\x60", selection)
        or not re.search(stem + r"\x60", selection)
        or not re.search(
            rf"/api/teach/course/department/\$\{{{_IDENTIFIER}\}}\x60", selection
        )
    ):
        raise ValueError("Cannot verify the school catalog browser's endpoint paths")
    table = re.escape(routes[0])
    assignments = list(re.finditer(rf"(?<![\w$]){table}\s*=(?![=>])\s*", script))
    mutation = re.search(
        rf"(?<![\w$]){table}\s*\.\s*"
        r"(?:push|pop|splice|shift|unshift|fill|reverse|sort|copyWithin)\s*\(",
        script,
    )
    if len(assignments) != 1 or mutation:
        raise ValueError("Cannot identify one static school catalog route table")
    entries = []
    known_keys: set[str] = set()
    for row in _literal_array(script, assignments[0].end()):
        try:
            kind = _SOURCE_KINDS[row["type"]]
            code, endpoint = row["code"], row["id"]
            name_cn, name_en = row["name"]["cn"], row["name"]["en"]
        except (KeyError, TypeError) as error:
            raise ValueError("Invalid row in the school catalog route table") from error
        if not all(
            isinstance(v, str) and v for v in (code, endpoint, name_cn, name_en)
        ):
            raise ValueError("Invalid route fields in the school catalog")
        if not re.fullmatch(r"[A-Za-z0-9]+(?:[+_-][A-Za-z0-9]+)*", code):
            raise ValueError(f"Invalid school catalog route {code!r}")
        if code in known_keys:
            raise ValueError(f"Duplicate school catalog route {code!r}")
        known_keys.add(code)
        if kind == "quality" and (code, endpoint) == ("quality", "quality"):
            source_ids = ("",)
        elif kind != "quality" and re.fullmatch(r"[0-9]+(?:\+[0-9]+)*", endpoint):
            source_ids = tuple(endpoint.split("+"))
        else:
            raise ValueError(f"Invalid source IDs for school catalog route {code!r}")
        if len(set(source_ids)) != len(source_ids):
            raise ValueError(f"Duplicate source IDs for catalog route {code!r}")
        entries.append(_CatalogEntry(code, kind, source_ids, name_cn, name_en))
    if not entries:
        raise ValueError("The school catalog route table is empty")
    return tuple(entries)


async def _discover_browser_registry(
    session: RequestSession,
    *,
    revalidate: bool = False,
) -> tuple[str, tuple[_CatalogEntry, ...]]:
    async def fetch(url: str):
        if not _browser_origin(url):
            raise ValueError("School catalog registry has an unexpected origin")
        headers = {"Cache-Control": "no-cache"} if revalidate else None
        response = await session.get(
            url, timeout=30_000, transient_retries=1, headers=headers
        )
        if not _browser_origin(response.url):
            raise ValueError("School catalog registry redirected outside its origin")
        return response.url, await response.text()

    page_url, html = await fetch(_CATALOG_URL)
    scripts = BeautifulSoup(html, "html.parser").select("script[type=module][src]")
    if len(scripts) != 1:
        raise ValueError("Cannot identify one module script on the school catalog page")
    script_url, script = await fetch(urljoin(page_url, str(scripts[0]["src"])))
    return script_url, _live_entries(script)


# The browser translates these source department group keys before merging.
_DEPARTMENT_GROUP_LABELS = {
    "学科群": "学科群基础课程",
    "专业核心": "专业核心课程",
    "专业方向": "专业方向课程",
}


# Columns after the common fetch and position fields, in insertion order.
_COLUMNS = {
    CATALOG_GROUP_ENTRIES: "route_key TEXT, route_position INTEGER, "
    "source_position INTEGER, name_cn TEXT, name_en TEXT",
    CATALOG_GROUP_ENDPOINTS: "payload_json TEXT",
    CATALOG_GROUPS: "group_path_json TEXT, parent_group_path_json TEXT, "
    "group_key TEXT, "
    "group_kind TEXT, group_position INTEGER, member_count INTEGER, "
    "display_label_cn TEXT",
    CATALOG_GROUP_MEMBERS: "group_path_json TEXT, course_code TEXT, "
    "member_position INTEGER, payload_json TEXT",
}


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _project(kind: str, id_: str, payload: object, url: str) -> tuple[list, list, list]:
    groups, members = [], []

    def walk(parent: tuple[str, ...], children: dict) -> None:
        for position, (label, value) in enumerate(children.items()):
            path = (*parent, label)
            if not isinstance(value, (dict, list)):
                raise ValueError(f"Invalid course group {path} from {url}")
            display = label if label or kind == "public" else "其他单位"
            if kind == "department":
                display = _DEPARTMENT_GROUP_LABELS.get(label, display)
            if kind == "public" and id_ == "66" and parent == ("体育选项",):
                display = f"体育选项 - {label}"
            is_array = isinstance(value, list)
            encoded = _json(path)
            group_kind = "array" if is_array else "object"
            member_count = len(value) if is_array else 0
            group = (
                encoded,
                _json(parent),
                label,
                group_kind,
                position,
                member_count,
                display,
            )
            groups.append(group)
            if not is_array:
                walk(path, value)
                continue
            for ordinal, row in enumerate(value):
                code = row.get("number") if isinstance(row, dict) else None
                if not isinstance(code, str) or not code or code != code.strip():
                    raise ValueError(f"Invalid course code in {path} from {url}")
                members.append((encoded, code, ordinal, _json(row)))

    if isinstance(payload, dict):
        walk((), payload)
    elif payload != []:
        raise ValueError(f"Expected a grouped object or empty array from {url}")
    return [(_json(payload),)], groups, members


def _requires_confirmation(conn, sources: dict) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name=?", (CATALOG_GROUP_ENTRIES,)
    )
    if not exists.fetchone():
        return False
    previous = conn.execute(
        f"SELECT source_kind, COUNT(DISTINCT route_key), COUNT(DISTINCT source_id), "
        f"COUNT(*) FROM {CATALOG_GROUP_ENTRIES} GROUP BY source_kind"
    )
    for kind, routes, endpoints, links in previous:
        selected = [rows for (k, _), rows in sources.items() if k == kind]
        if (
            len(selected) < endpoints
            or sum(map(len, selected)) < links
            or len({row[0] for rows in selected for row in rows}) < routes
        ):
            return True
    return False


def _persist(store: SQLiteModelStore, registry_url: str, snapshots: list) -> None:
    conn = store.conn
    with conn:
        counts = {f"{CATALOG_GROUPS}_registry_source": registry_url}
        inserts = {}
        for table, fields in _COLUMNS.items():
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ("
                "store_id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "fetch_id INTEGER NOT NULL, "
                "position INTEGER NOT NULL, source_kind TEXT, "
                f"source_id TEXT, {fields})"
            )
            extra = [field.strip().split()[0] for field in fields.split(",")]
            columns = ", ".join(
                ("fetch_id", "position", "source_kind", "source_id", *extra)
            )
            parameters = ", ".join("?" for _ in range(4 + len(extra)))
            inserts[table] = f"INSERT INTO {table} ({columns}) VALUES ({parameters})"
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM upstream_fetches WHERE source = ?", (CATALOG_GROUPS,))
        for (kind, id_), url, rows in snapshots:
            fetch_id = store.record_fetch(
                source=CATALOG_GROUPS,
                method="GET",
                url=url,
                context={"source_kind": kind, "source_id": id_},
            )
            for (table, sql), records in zip(inserts.items(), rows, strict=True):
                data = (
                    (fetch_id, position, kind, id_, *row)
                    for position, row in enumerate(records)
                )
                conn.executemany(sql, data)
                key = f"{table}_count"
                counts[key] = counts.get(key, 0) + len(records)
        table = CATALOG_GROUP_MEMBERS
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {table}_course_code_idx "
            f"ON {table}(course_code)"
        )
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {table}_source_path_idx "
            f"ON {table}(source_kind, source_id, group_path_json)"
        )
        store.put_metadata(counts)


async def _fetch_in_batches[T, R](
    fetch: Callable[[T], Awaitable[R]],
    items: Iterable[T],
    concurrency: int,
    interval: float,
) -> list[R]:
    results = []
    for batch in batched(items, concurrency, strict=False):
        if results and interval:
            await asyncio.sleep(interval)
        results.extend(await asyncio.gather(*(fetch(item) for item in batch)))
    return results


async def refresh_catalog_groups(
    session: RequestSession, store: SQLiteModelStore
) -> None:
    """Fetch each endpoint once and replace the classification snapshot atomically."""
    registry_url, entries = await _discover_browser_registry(session)
    sources = defaultdict(list)
    for position, entry in enumerate(entries):
        for ordinal, id_ in enumerate(entry.source_ids):
            sources[entry.source_kind, id_].append(
                (entry.route_key, position, ordinal, entry.name_cn, entry.name_en)
            )
    if _requires_confirmation(store.conn, sources):
        registry_url, confirmed = await _discover_browser_registry(
            session, revalidate=True
        )
        if confirmed != entries:
            raise ValueError("Smaller school catalog registry could not be confirmed")

    async def fetch(source: tuple[str, str]) -> tuple:
        kind, id_ = source
        url = f"{_COURSE_URL}/{kind}/{id_}".rstrip("/")
        payload = await session.get_json(url, timeout=60_000, transient_retries=1)
        return source, url, (sources[source], *_project(kind, id_, payload, url))

    snapshots = await _fetch_in_batches(
        fetch, sources, _MAX_CONCURRENT_REQUESTS, _BATCH_INTERVAL_SECONDS
    )
    if not any(rows[-1] for _, _, rows in snapshots):
        raise ValueError("The official course browser returned no group memberships")
    _persist(store, registry_url, snapshots)
