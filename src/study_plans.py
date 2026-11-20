"""Refresh public study plans and keep their course associations in the snapshot."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, Strict, TypeAdapter

from .catalog_groups import _fetch_in_batches
from .sqlite_store import SQLiteModelStore
from .utils.auth import RequestSession

_SOURCE = "https://catalog.ustc.edu.cn/api/teach"
PROGRAM_TREE_URL = f"{_SOURCE}/program/tree"
PROGRAM_INFO_URL = f"{_SOURCE}/program/info"
COURSE_MODULE_URL = f"{_SOURCE}/course-module/info"
PROGRAM_TREE_TABLE = "catalog_teach_program_tree"
PROGRAM_INFO_TABLE = "catalog_teach_program_info"
COURSE_MODULE_TABLE = "catalog_teach_course_module_info"
PROGRAM_COURSES_TABLE = "catalog_teach_program_courses"
REFRESH_AGE = timedelta(days=14)
FETCH_CONCURRENCY = 4
FETCH_INTERVAL_SECONDS = 0.22


type PlanSummary = tuple[int, int, str, str | None, str]
type Id = Annotated[PositiveInt, Strict()]


class _Module(BaseModel):
    id: Id
    public: Id | None = None
    courses: list[dict] | None = None


class _Node(BaseModel):
    module: _Module = Field(alias="self")
    children: list[_Node] | None = None


_NODES = TypeAdapter(list[_Node])


class Resource(NamedTuple):
    payload_json: str
    fetch_id: int | None = None
    fresh: bool = False
    summary_json: str | None = None


def _json(value: object, *, sort_keys: bool = False) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=sort_keys,
        allow_nan=False,
    )


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


class _TreeRecord(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: Id


class _Plan(_TreeRecord):
    grade: str
    train_type: str | None = Field(default=None, alias="trainType")


class _Major(_TreeRecord):
    programs: list[_Plan]


class _Department(_TreeRecord):
    majors: dict[str, _Major]


_TREE = TypeAdapter(dict[str, _Department])


def _index(tree: object) -> dict[int, PlanSummary]:
    programs = {}
    for department in _TREE.validate_python(tree).values():
        department_summary = department.model_dump(
            exclude={"majors"}, exclude_unset=True
        )
        for major in department.majors.values():
            major_summary = major.model_dump(exclude={"programs"}, exclude_unset=True)
            for plan in major.programs:
                summary = _json(
                    {
                        "department": department_summary,
                        "major": major_summary,
                        "program": plan.model_dump(exclude_unset=True, by_alias=True),
                    },
                    sort_keys=True,
                )
                entry = (department.id, major.id, plan.grade, plan.train_type, summary)
                if programs.setdefault(plan.id, entry) != entry:
                    raise ValueError(f"Plan {plan.id} has conflicting tree entries")
    if not programs:
        raise ValueError("The public program index contained no study plans")
    return programs


def _references(nodes: Iterable[_Node]) -> Iterator[int]:
    for node in nodes:
        if node.module.public is not None and node.module.public != node.module.id:
            yield node.module.public
        yield from _references(node.children or ())


async def _fetch(session: RequestSession, url: str) -> object:
    return await session.get_json(url, timeout=30_000, transient_retries=1)


async def _fetch_resources(
    session: RequestSession,
    url: str,
    ids: Iterable[int],
) -> dict[int, Resource]:
    async def fetch(id_: int) -> tuple[int, Resource]:
        address = f"{url}/{id_}"
        return id_, Resource(_json(_object(await _fetch(session, address), address)))

    return dict(
        await _fetch_in_batches(
            fetch,
            sorted(ids),
            FETCH_CONCURRENCY,
            FETCH_INTERVAL_SECONDS * FETCH_CONCURRENCY,
        )
    )


def _cached(
    store: SQLiteModelStore, table: str, id_column: str, cutoff: datetime
) -> dict[int, Resource]:
    conn = store.conn
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone():
        return {}

    summary = "data.summary_json" if table == PROGRAM_INFO_TABLE else "NULL"
    return {
        id_: Resource(payload, fetch_id, bool(fresh), index)
        for id_, payload, fetch_id, fresh, index in conn.execute(
            f"SELECT data.{id_column}, data.payload_json, fetch.id, "
            "(fetch.fetched_at LIKE '%+__:__' OR fetch.fetched_at LIKE '%-__:__' "
            "OR fetch.fetched_at LIKE '%Z') AND "
            f"julianday(fetch.fetched_at) >= julianday(?), {summary} "
            f"FROM {table} data JOIN upstream_fetches fetch "
            "ON fetch.id = data.fetch_id",
            (cutoff.isoformat(),),
        )
    }


def _memberships(
    nodes: Iterable[_Node],
    public_modules: dict[int, _Node],
    path: tuple[int, ...] = (),
    public_module_id: int | None = None,
) -> Iterator[tuple]:
    for node in nodes:
        module = node.module
        module_path = (*path, module.id)
        path_json = _json(module_path)
        for course_position, item in enumerate(module.courses or ()):
            source_course = item.get("course")
            if source_course is None:
                continue
            course = _object(source_course, "module membership course")
            code = course.get("code")
            if code is None or code == "":
                continue
            if not isinstance(code, str):
                raise ValueError("Study plan course.code must be text")
            yield (
                code,
                course.get("id"),
                module.id,
                public_module_id,
                path_json,
                course_position,
                item.get("compulsory"),
                _json(item.get("terms")),
            )
        yield from _memberships(
            node.children or (), public_modules, module_path, public_module_id
        )
        public_id = module.public
        if public_id is not None and public_id not in module_path:
            yield from _memberships(
                [public_modules[public_id]], public_modules, module_path, public_id
            )


def _create_tables(store: SQLiteModelStore) -> None:
    fetch = "fetch_id INTEGER NOT NULL REFERENCES upstream_fetches(id)"
    # Execute each statement individually to keep the refresh savepoint open.
    for statement in (
        f"CREATE TABLE IF NOT EXISTS {PROGRAM_TREE_TABLE} ("
        f"id INTEGER PRIMARY KEY CHECK (id = 1), {fetch}, payload_json TEXT NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS {PROGRAM_INFO_TABLE} ("
        "program_id INTEGER PRIMARY KEY, "
        f"{fetch}, department_id INTEGER NOT NULL, major_id INTEGER NOT NULL, "
        "grade TEXT NOT NULL, train_type TEXT, summary_json TEXT NOT NULL, "
        "payload_json TEXT NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS {COURSE_MODULE_TABLE} ("
        f"module_id INTEGER PRIMARY KEY, {fetch}, payload_json TEXT NOT NULL)",
        f"CREATE TABLE IF NOT EXISTS {PROGRAM_COURSES_TABLE} ("
        f"program_id INTEGER NOT NULL REFERENCES {PROGRAM_INFO_TABLE}(program_id), "
        "position INTEGER NOT NULL, course_code TEXT NOT NULL, course_id INTEGER, "
        "module_id INTEGER NOT NULL, public_module_id INTEGER REFERENCES "
        f"{COURSE_MODULE_TABLE}(module_id), module_path_json TEXT NOT NULL, "
        "course_position INTEGER NOT NULL, compulsory INTEGER, "
        "terms_json TEXT NOT NULL, "
        "PRIMARY KEY (program_id, position))",
        f"CREATE INDEX IF NOT EXISTS {PROGRAM_COURSES_TABLE}_code "
        f"ON {PROGRAM_COURSES_TABLE}(course_code, program_id)",
    ):
        store.conn.execute(statement)


def _save_resources(
    store: SQLiteModelStore,
    table: str,
    url: str,
    resources: dict[int, Resource],
    previous: dict[int, Resource],
    index: dict[int, PlanSummary] | None = None,
) -> None:
    conn = store.conn
    id_column = "module_id" if index is None else "program_id"
    conn.executemany(
        f"DELETE FROM {table} WHERE {id_column} = ?",
        ((id_,) for id_ in previous.keys() - resources.keys()),
    )

    def changed_rows():
        for id_, resource in resources.items():
            if resource.fetch_id is None:
                fetch = store.record_fetch(
                    source=table, method="GET", url=f"{url}/{id_}"
                )
                yield (
                    id_,
                    fetch,
                    *(index[id_] if index else ()),
                    resource.payload_json,
                )

    slots = "?, ?, ?" if index is None else "?, ?, ?, ?, ?, ?, ?, ?"
    conn.executemany(f"INSERT OR REPLACE INTO {table} VALUES ({slots})", changed_rows())


async def refresh_study_plans(session: RequestSession, store: SQLiteModelStore) -> None:
    """Refresh the public program index, plan details and reachable public modules."""
    cutoff = datetime.now(UTC) - REFRESH_AGE
    tree = await _fetch(session, PROGRAM_TREE_URL)
    index = _index(tree)
    old_programs = _cached(store, PROGRAM_INFO_TABLE, "program_id", cutoff)
    old_modules = _cached(store, COURSE_MODULE_TABLE, "module_id", cutoff)
    if (
        old_programs.keys() - index.keys()
        and _index(await _fetch(session, PROGRAM_TREE_URL)) != index
    ):
        raise ValueError("A smaller public program index could not be confirmed")

    programs = {
        id_: previous
        for id_, previous in old_programs.items()
        if id_ in index and previous.summary_json == index[id_][-1] and previous.fresh
    }
    missing = index.keys() - programs.keys()
    programs.update(await _fetch_resources(session, PROGRAM_INFO_URL, missing))
    plan_roots = {
        id_: _NODES.validate_python(json.loads(resource.payload_json).get("moduleTree"))
        for id_, resource in programs.items()
    }
    pending = {ref for roots in plan_roots.values() for ref in _references(roots)}
    modules, module_payloads = {}, {}
    while pending:
        reusable = {
            id_: old_modules[id_]
            for id_ in pending
            if id_ in old_modules and old_modules[id_].fresh
        }
        missing = pending - reusable.keys()
        batch = reusable | await _fetch_resources(session, COURSE_MODULE_URL, missing)
        modules.update(batch)
        decoded = {
            id_: _Node.model_validate_json(resource.payload_json)
            for id_, resource in batch.items()
        }
        if any(id_ != node.module.id for id_, node in decoded.items()):
            raise ValueError("Public module response does not match the requested ID")
        module_payloads.update(decoded)
        pending = set(_references(decoded.values())) - modules.keys()

    changed = old_programs != programs or old_modules != modules
    conn = store.conn
    conn.execute("SAVEPOINT public_study_plans")
    try:
        _create_tables(store)
        tree_fetch = store.record_fetch(
            source=PROGRAM_TREE_TABLE, method="GET", url=PROGRAM_TREE_URL
        )
        conn.execute(
            f"INSERT OR REPLACE INTO {PROGRAM_TREE_TABLE} VALUES (1, ?, ?)",
            (tree_fetch, _json(tree)),
        )
        if changed:
            conn.execute(f"DELETE FROM {PROGRAM_COURSES_TABLE}")
        plan_args = PROGRAM_INFO_TABLE, PROGRAM_INFO_URL, programs, old_programs, index
        module_args = COURSE_MODULE_TABLE, COURSE_MODULE_URL, modules, old_modules
        _save_resources(store, *plan_args)
        _save_resources(store, *module_args)
        if changed:
            conn.executemany(
                f"INSERT INTO {PROGRAM_COURSES_TABLE} "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    (id_, position, *row)
                    for id_, roots in plan_roots.items()
                    for position, row in enumerate(_memberships(roots, module_payloads))
                ),
            )
        for table in (PROGRAM_TREE_TABLE, PROGRAM_INFO_TABLE, COURSE_MODULE_TABLE):
            conn.execute(
                "DELETE FROM upstream_fetches WHERE source = ? "
                f"AND id NOT IN (SELECT fetch_id FROM {table})",
                (table,),
            )
        counts = {PROGRAM_INFO_TABLE: len(programs), COURSE_MODULE_TABLE: len(modules)}
        counts[PROGRAM_COURSES_TABLE] = conn.execute(
            f"SELECT COUNT(*) FROM {PROGRAM_COURSES_TABLE}"
        ).fetchone()[0]
        store.put_metadata({f"{table}_count": n for table, n in counts.items()})
    except BaseException:
        conn.execute("ROLLBACK TO public_study_plans")
        raise
    finally:
        conn.execute("RELEASE public_study_plans")
