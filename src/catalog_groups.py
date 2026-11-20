"""Fetch the exact classification sources used by the public course browser."""

from __future__ import annotations

import asyncio
import json
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from itertools import batched
from typing import Literal
from urllib.parse import urljoin, urlsplit

from pydantic import BaseModel, RootModel

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

type SourceKind = Literal["public", "department", "quality"]


@dataclass(frozen=True)
class _CatalogEntry:
    route_key: str
    source_kind: SourceKind
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
_SOURCE_KINDS: dict[str, SourceKind] = {
    "generic": "public",
    "school": "department",
    "quality": "quality",
}


class _ModuleScripts(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        fields = dict(attrs)
        if tag == "script" and fields.get("type") == "module" and fields.get("src"):
            self.sources.append(fields["src"] or "")


def _browser_origin(url: str) -> bool:
    actual = urlsplit(url)
    return (actual.scheme, actual.netloc) == ("https", "catalog.ustc.edu.cn")


def _literal_array(script: str, start: int) -> list:
    tokens: list[str] = []
    depth = 0
    # The reviewed array is about 3 KiB. Bound the literal parser if the
    # browser starts constructing routes with arbitrary JavaScript.
    end = start + 65_536
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
    dispatches = list(_DISPATCH.finditer(script))
    if len(dispatches) != 1:
        raise ValueError("Cannot verify the school catalog browser's endpoint dispatch")
    dispatch = dispatches[0]
    cases = _DISPATCH_CASE.findall(dispatch["cases"])
    if {kind: endpoint for kind, _, endpoint in cases} != _SOURCE_KINDS:
        raise ValueError("Unexpected school catalog endpoint kind or duplicate case")
    kind_variables = {variable for _, variable, _ in cases}
    if len(kind_variables) != 1:
        raise ValueError("Cannot verify the school catalog endpoint selection")
    selection = script[dispatch.end() : dispatch.end() + 2_500]
    lookups = list(_ROUTE_LOOKUP.finditer(selection))
    routes = {match["table"] for match in lookups}
    endpoint_kind = re.escape(kind_variables.pop())
    stem = rf"/api/teach/course/\$\{{{endpoint_kind}\}}"
    if (
        len(lookups) != 2
        or len(routes) != 1
        or not re.search(stem + rf"/\$\{{{_IDENTIFIER}\}}\x60", selection)
        or not re.search(stem + r"\x60", selection)
        or not re.search(
            rf"/api/teach/course/department/\$\{{{_IDENTIFIER}\}}\x60", selection
        )
    ):
        raise ValueError("Cannot verify the school catalog browser's endpoint paths")
    table = re.escape(routes.pop())
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
            isinstance(value, str) and value
            for value in (code, endpoint, name_cn, name_en)
        ):
            raise ValueError(
                "Invalid identity or label in the school catalog route table"
            )
        if not re.fullmatch(r"[A-Za-z0-9]+(?:[+_-][A-Za-z0-9]+)*", code):
            raise ValueError(f"Invalid school catalog route {code!r}")
        if code in known_keys:
            raise ValueError(f"Duplicate school catalog route {code!r}")
        known_keys.add(code)
        if kind == "quality":
            if code != "quality" or endpoint != "quality":
                raise ValueError(
                    "Unexpected quality-course route in the school catalog"
                )
            source_ids = ("",)
        else:
            if not re.fullmatch(r"[0-9]+(?:\+[0-9]+)*", endpoint):
                raise ValueError(
                    f"Invalid source IDs for school catalog route {code!r}"
                )
            source_ids = tuple(endpoint.split("+"))
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
    headers = {"Cache-Control": "no-cache"} if revalidate else None
    page = await session.get(
        _CATALOG_URL, timeout=30_000, transient_retries=1, headers=headers
    )
    if not _browser_origin(page.url):
        raise ValueError("School catalog page redirected outside the catalog origin")
    parser = _ModuleScripts()
    parser.feed(await page.text())
    if len(parser.sources) != 1:
        raise ValueError("Cannot identify one module script on the school catalog page")
    script_url = urljoin(page.url, parser.sources[0])
    if not _browser_origin(script_url):
        raise ValueError("School catalog module script has an unexpected origin")
    response = await session.get(
        script_url, timeout=30_000, transient_retries=1, headers=headers
    )
    if not _browser_origin(response.url):
        raise ValueError(
            "School catalog module script redirected outside the catalog origin"
        )
    try:
        actual = _live_entries(await response.text())
    except ValueError as error:
        raise ValueError(
            f"Cannot verify catalog routes from {response.url}: {error}"
        ) from error
    return response.url, actual


# The browser translates these source department group keys before merging.
_DEPARTMENT_GROUP_LABELS = {
    "学科群": "学科群基础课程",
    "专业核心": "专业核心课程",
    "专业方向": "专业方向课程",
}


@dataclass(frozen=True)
class _Source:
    source_kind: SourceKind
    source_id: str

    @property
    def url(self) -> str:
        return f"{_COURSE_URL}/{self.source_kind}/{self.source_id}".rstrip("/")

    @property
    def fields(self) -> dict[str, str]:
        return {"source_kind": self.source_kind, "source_id": self.source_id}


class _SourceRow(BaseModel):
    source_kind: SourceKind
    source_id: str


class _EntryRow(_SourceRow):
    route_key: str
    route_position: int
    source_position: int
    name_cn: str
    name_en: str


class _EndpointRow(_SourceRow):
    payload_json: str


class _GroupRow(_SourceRow):
    group_path_json: str
    parent_group_path_json: str
    group_key: str
    # Empty arrays and empty objects have different meanings in the source.
    group_kind: Literal["object", "array"]
    group_position: int
    member_count: int
    display_label_cn: str


class _MemberRow(_SourceRow):
    group_path_json: str
    course_code: str
    member_position: int
    payload_json: str


class _EntryResponse(RootModel[list[_EntryRow]]):
    pass


class _EndpointResponse(RootModel[list[_EndpointRow]]):
    pass


class _GroupResponse(RootModel[list[_GroupRow]]):
    pass


class _MemberResponse(RootModel[list[_MemberRow]]):
    pass


@dataclass
class _SourceSnapshot:
    source: _Source
    endpoint: _EndpointRow
    groups: list[_GroupRow]
    members: list[_MemberRow]


def _as_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _display_label(source: _Source, path: tuple[str, ...]) -> str:
    label = path[-1]
    if source.source_kind == "department":
        return _DEPARTMENT_GROUP_LABELS.get(label, label) or "其他单位"
    if source.source_kind == "quality":
        return label or "其他单位"
    if source.source_id == "66" and len(path) == 2 and path[0] == "体育选项":
        return f"体育选项 - {label}"
    return label


def _parse_source(source: _Source, payload: object) -> _SourceSnapshot:
    if not isinstance(payload, dict) and payload != []:
        raise ValueError(f"Expected a grouped object or empty array from {source.url}")
    snapshot = _SourceSnapshot(
        source=source,
        endpoint=_EndpointRow(**source.fields, payload_json=_as_json(payload)),
        groups=[],
        members=[],
    )

    def add_children(parent: tuple[str, ...], groups: Mapping[str, object]) -> None:
        for position, (key, value) in enumerate(groups.items()):
            path = (*parent, key)
            encoded_path = _as_json(path)
            if not isinstance(value, (dict, list)):
                raise ValueError(
                    f"Invalid course group {encoded_path} from {source.url}"
                )
            snapshot.groups.append(
                _GroupRow(
                    **source.fields,
                    group_path_json=encoded_path,
                    parent_group_path_json=_as_json(parent),
                    group_key=key,
                    group_kind="array" if isinstance(value, list) else "object",
                    group_position=position,
                    member_count=len(value) if isinstance(value, list) else 0,
                    display_label_cn=_display_label(source, path),
                )
            )
            if isinstance(value, dict):
                add_children(path, value)
                continue

            for ordinal, row in enumerate(value):
                code = row.get("number") if isinstance(row, dict) else None
                if not isinstance(code, str) or not code or code != code.strip():
                    raise ValueError(
                        f"Invalid course code in {encoded_path} from {source.url}"
                    )
                snapshot.members.append(
                    _MemberRow(
                        **source.fields,
                        group_path_json=encoded_path,
                        course_code=code,
                        member_position=ordinal,
                        payload_json=_as_json(row),
                    )
                )

    if isinstance(payload, dict):
        add_children((), payload)
    return snapshot


def _entry_sources(
    entries: tuple[_CatalogEntry, ...],
) -> dict[_Source, list[_EntryRow]]:
    sources: dict[_Source, list[_EntryRow]] = defaultdict(list)
    for route_position, entry in enumerate(entries):
        for source_position, source_id in enumerate(entry.source_ids):
            source = _Source(entry.source_kind, source_id)
            sources[source].append(
                _EntryRow(
                    **source.fields,
                    route_key=entry.route_key,
                    route_position=route_position,
                    source_position=source_position,
                    name_cn=entry.name_cn,
                    name_en=entry.name_en,
                )
            )
    return sources


def _scope_shrink(
    store: SQLiteModelStore,
    entries: tuple[_CatalogEntry, ...],
    sources: Mapping[_Source, list[_EntryRow]],
) -> str | None:
    if not store.conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (CATALOG_GROUP_ENTRIES,),
    ).fetchone():
        return None
    previous = store.conn.execute(
        f"SELECT source_kind, COUNT(DISTINCT route_key), COUNT(DISTINCT source_id), "
        f"COUNT(*) FROM {CATALOG_GROUP_ENTRIES} GROUP BY source_kind"
    )
    for kind, old_routes, old_sources, old_links in previous:
        routes = sum(entry.source_kind == kind for entry in entries)
        endpoints = [source for source in sources if source.source_kind == kind]
        links = sum(len(sources[source]) for source in endpoints)
        if routes < old_routes or len(endpoints) < old_sources or links < old_links:
            return (
                f"School catalog registry shrank for {kind}: "
                f"routes {old_routes} → {routes}, "
                f"endpoints {old_sources} → {len(endpoints)}, "
                f"links {old_links} → {links}"
            )
    return None


async def _fetch_source(session: RequestSession, source: _Source) -> _SourceSnapshot:
    payload = await session.get_json(source.url, timeout=60_000, transient_retries=1)
    return _parse_source(source, payload)


def _persist(
    store: SQLiteModelStore,
    sources: Mapping[_Source, list[_EntryRow]],
    snapshots: list[_SourceSnapshot],
    registry_url: str,
) -> None:
    with store.conn:
        for table, model in (
            (CATALOG_GROUP_ENTRIES, _EntryResponse),
            (CATALOG_GROUP_ENDPOINTS, _EndpointResponse),
            (CATALOG_GROUPS, _GroupResponse),
            (CATALOG_GROUP_MEMBERS, _MemberResponse),
        ):
            store.register_response_model(table_name=table, response_model=model)
        old_fetches = [
            fetch_id
            for (fetch_id,) in store.conn.execute(
                "SELECT id FROM upstream_fetches WHERE source = ?", (CATALOG_GROUPS,)
            )
        ]
        store.delete_fetches(old_fetches)

        counts: dict[str, int | str] = {
            f"{CATALOG_GROUPS}_registry_source": registry_url
        }
        for snapshot in snapshots:
            source = snapshot.source
            fetch_id = store.record_fetch(
                source=CATALOG_GROUPS,
                method="GET",
                url=source.url,
                context=source.fields,
            )
            for table, response in (
                (CATALOG_GROUP_ENTRIES, _EntryResponse(sources[source])),
                (CATALOG_GROUP_ENDPOINTS, _EndpointResponse([snapshot.endpoint])),
                (CATALOG_GROUPS, _GroupResponse(snapshot.groups)),
                (CATALOG_GROUP_MEMBERS, _MemberResponse(snapshot.members)),
            ):
                key = f"{table}_count"
                count = store.store_response(
                    table_name=table, response=response, fetch_id=fetch_id
                )
                counts[key] = int(counts.get(key, 0)) + count

        store.conn.execute(
            f"CREATE INDEX IF NOT EXISTS {CATALOG_GROUP_MEMBERS}_course_code_idx "
            f"ON {CATALOG_GROUP_MEMBERS}(course_code)"
        )
        store.conn.execute(
            f"CREATE INDEX IF NOT EXISTS {CATALOG_GROUP_MEMBERS}_source_path_idx "
            f"ON {CATALOG_GROUP_MEMBERS}(source_kind, source_id, group_path_json)"
        )
        store.put_metadata(counts)


async def refresh_catalog_groups(
    session: RequestSession, store: SQLiteModelStore
) -> None:
    """Fetch each classification endpoint once and replace the snapshot as a unit."""
    registry_url, entries = await _discover_browser_registry(session)
    sources = _entry_sources(entries)
    shrink = _scope_shrink(store, entries, sources)
    if shrink:
        confirmed_url, confirmed = await _discover_browser_registry(
            session, revalidate=True
        )
        if confirmed != entries:
            raise ValueError(
                f"{shrink}; the second registry did not confirm the change"
            )
        registry_url = confirmed_url
    snapshots: list[_SourceSnapshot] = []
    for sources_batch in batched(sources, _MAX_CONCURRENT_REQUESTS, strict=False):
        if snapshots:
            await asyncio.sleep(_BATCH_INTERVAL_SECONDS)
        snapshots.extend(
            await asyncio.gather(
                *(_fetch_source(session, source) for source in sources_batch)
            )
        )
    if not snapshots or not any(snapshot.members for snapshot in snapshots):
        raise ValueError("The official course browser returned no group memberships")
    _persist(store, sources, snapshots, registry_url)
