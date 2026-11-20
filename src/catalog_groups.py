"""Fetch the exact classification sources used by the public course browser."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import batched
from typing import Literal

from pydantic import BaseModel, RootModel

from .sqlite_store import SQLiteModelStore
from .utils.auth import RequestSession

CATALOG_GROUP_ENTRIES = "catalog_teach_course_group_entries"
CATALOG_GROUP_ENDPOINTS = "catalog_teach_course_group_endpoints"
CATALOG_GROUPS = "catalog_teach_course_groups"
CATALOG_GROUP_MEMBERS = "catalog_teach_course_group_members"

_COURSE_URL = "https://catalog.ustc.edu.cn/api/teach/course"
_REGISTRY_SOURCE_URL = "https://catalog.ustc.edu.cn/assets/index-DeAsVxMB.js"
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


# Vetted against the official browser's catalog route registry on 2026-11-20.
# The keys after /catalog/ are UI routes. The ordered tuples are endpoint
# identifiers, including compound entries; they are not department codes.
_CATALOG_ENTRIES = (
    _CatalogEntry("ma", "public", ("43",), "数学类", "Mathematics"),
    _CatalogEntry("ph", "public", ("45",), "物理类", "Physics"),
    _CatalogEntry("fl", "public", ("59",), "英语类", "Foreign Language"),
    _CatalogEntry(
        "hs+ps", "public", ("58", "63"), "人文、思政类", "Humanities & Politics"
    ),
    _CatalogEntry("pe", "public", ("66",), "体育类", "Physical Education"),
    _CatalogEntry(
        "cs+es+in",
        "public",
        ("49", "41", "47", "102"),
        "计算机、电子类",
        "Computer & Electronics",
    ),
    _CatalogEntry(
        "ch+ms+bi", "public", ("44", "54", "53"), "化学、生物类", "Chemistry & Biology"
    ),
    _CatalogEntry(
        "ge+gp+ae+en",
        "public",
        ("50", "52", "55", "46"),
        "地球、环境类",
        "Earth & Environmental",
    ),
    _CatalogEntry("quality", "quality", ("",), "综合素质类", "Comprehensive Abilities"),
    _CatalogEntry(
        "001",
        "department",
        ("2", "5", "6", "61"),
        "数学科学学院",
        "School of Mathematical Sciences",
    ),
    _CatalogEntry(
        "203",
        "department",
        ("87", "56", "62", "63", "77", "100", "122"),
        "物理学院",
        "School of Physical Sciences",
    ),
    _CatalogEntry(
        "204",
        "department",
        ("88", "71", "72", "73"),
        "管理学院",
        "School of Management",
    ),
    _CatalogEntry(
        "206",
        "department",
        ("89", "57", "68", "70", "74", "75", "182"),
        "化学与材料科学学院",
        "School of Chemistry and Materials Science",
    ),
    _CatalogEntry(
        "207",
        "department",
        ("90", "66", "76", "101", "102"),
        "生命科学学院",
        "School of Life Sciences",
    ),
    _CatalogEntry(
        "208",
        "department",
        ("91", "18", "43", "46"),
        "地球和空间科学学院",
        "School of Earth and Space Sciences",
    ),
    _CatalogEntry(
        "209",
        "department",
        ("92", "3", "4", "69", "99"),
        "工程科学学院",
        "School of Engineering Science",
    ),
    _CatalogEntry(
        "210",
        "department",
        ("93", "23", "65", "67", "78"),
        "信息科学技术学院",
        "School of Information Science and Technology",
    ),
    _CatalogEntry(
        "211",
        "department",
        ("94", "58", "80"),
        "人文与社会科学学院",
        "School of Humanities and Social Science",
    ),
    _CatalogEntry(
        "215",
        "department",
        ("96", "64"),
        "计算机科学与技术学院",
        "School of Computer Science and Technology",
    ),
    _CatalogEntry(
        "216", "department", ("15",), "公共事务学院", "School of Public Affairs"
    ),
    _CatalogEntry(
        "221",
        "department",
        ("146",),
        "网络空间安全学院",
        "School of Cyberspace Security",
    ),
    _CatalogEntry(
        "229", "department", ("142",), "大数据学院", "School of Data Science"
    ),
    _CatalogEntry(
        "910",
        "department",
        ("145",),
        "生命科学与医学部",
        "School of Life Sciences and Medicine",
    ),
)

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


def _entry_sources() -> dict[_Source, list[_EntryRow]]:
    sources: dict[_Source, list[_EntryRow]] = defaultdict(list)
    for route_position, entry in enumerate(_CATALOG_ENTRIES):
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


async def _fetch_source(session: RequestSession, source: _Source) -> _SourceSnapshot:
    payload = await session.get_json(source.url, timeout=60_000, transient_retries=1)
    return _parse_source(source, payload)


def _persist(
    store: SQLiteModelStore,
    sources: Mapping[_Source, list[_EntryRow]],
    snapshots: list[_SourceSnapshot],
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
            f"{CATALOG_GROUPS}_registry_source": _REGISTRY_SOURCE_URL
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
    sources = _entry_sources()
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
    _persist(store, sources, snapshots)
