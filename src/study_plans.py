"""Refresh public study plans and keep their course associations in the snapshot."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

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
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Program:
    department_id: int
    major_id: int
    grade: str
    train_type: str | None
    summary_json: str


@dataclass(frozen=True)
class Resource:
    payload_json: str
    fetch_id: int | None = None
    fetched_at: str | None = None
    summary_json: str | None = None

    def is_fresh(self, cutoff: datetime) -> bool:
        if self.fetched_at is None:
            return False
        try:
            timestamp = datetime.fromisoformat(self.fetched_at)
        except ValueError:
            return False
        return timestamp.tzinfo is not None and timestamp >= cutoff


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


def _array(value: object, label: str) -> list:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON array")
    return value


def _positive_id(value: object, label: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{label} must be a positive ID")
    return value


def _index(tree: object) -> dict[int, Program]:
    programs: dict[int, Program] = {}
    for department in _object(tree, "program tree").values():
        department = _object(department, "program department")
        department_id = _positive_id(department.get("id"), "department.id")
        department_fields = {k: v for k, v in department.items() if k != "majors"}
        for major in _object(department.get("majors"), "department.majors").values():
            major = _object(major, "program major")
            major_id = _positive_id(major.get("id"), "major.id")
            major_fields = {k: v for k, v in major.items() if k != "programs"}
            for program in _array(major.get("programs"), "major.programs"):
                program = _object(program, "program summary")
                program_id = _positive_id(program.get("id"), "program.id")
                grade = program.get("grade")
                train_type = program.get("trainType")
                if not isinstance(grade, str) or (
                    train_type is not None and not isinstance(train_type, str)
                ):
                    raise ValueError(
                        f"Invalid grade or training type for plan {program_id}"
                    )
                entry = Program(
                    department_id,
                    major_id,
                    grade,
                    train_type,
                    _json(
                        {
                            "department": department_fields,
                            "major": major_fields,
                            "program": program,
                        },
                        sort_keys=True,
                    ),
                )
                if program_id in programs and entry != programs[program_id]:
                    raise ValueError(f"Plan {program_id} has conflicting tree entries")
                programs[program_id] = entry
    if not programs:
        raise ValueError("The public program index contained no study plans")
    return programs


def _node_fields(node: dict) -> tuple[dict, int, int | None, list, list]:
    node = _object(node, "module node")
    context = _object(node.get("self"), "module.self")
    module_id = _positive_id(context.get("id"), "module.self.id")
    public_id = context.get("public")
    if public_id is not None:
        public_id = _positive_id(public_id, "module.self.public")
    children = node.get("children")
    courses = context.get("courses")
    return (
        context,
        module_id,
        public_id,
        [] if children is None else _array(children, "module.children"),
        [] if courses is None else _array(courses, "module.self.courses"),
    )


def _references(roots: list) -> set[int]:
    pending = list(roots)
    refs: set[int] = set()
    while pending:
        _, module_id, public_id, children, _ = _node_fields(pending.pop())
        if public_id is not None and public_id != module_id:
            refs.add(public_id)
        pending.extend(children)
    return refs


async def _fetch(session: RequestSession, url: str) -> object:
    return await session.get_json(url, timeout=30_000, transient_retries=1)


async def _fetch_resources(
    session: RequestSession,
    url: str,
    ids: Iterable[int],
) -> dict[int, Resource]:
    pending = iter(sorted(ids))
    resources: dict[int, Resource] = {}
    pace = asyncio.Lock()

    async def worker() -> None:
        for id_ in pending:
            if FETCH_INTERVAL_SECONDS:
                async with pace:
                    await asyncio.sleep(FETCH_INTERVAL_SECONDS)
            payload = _object(await _fetch(session, f"{url}/{id_}"), f"{url}/{id_}")
            if url == PROGRAM_INFO_URL:
                _array(payload.get("moduleTree"), f"plan {id_} moduleTree")
            else:
                actual_id = _node_fields(payload)[1]
                if actual_id != id_:
                    raise ValueError(
                        f"Requested public module {id_}, received {actual_id}"
                    )
            resources[id_] = Resource(_json(payload))

    async with asyncio.TaskGroup() as tasks:
        for _ in range(FETCH_CONCURRENCY):
            tasks.create_task(worker())
    return resources


def _cached(store: SQLiteModelStore, table: str, id_column: str) -> dict[int, Resource]:
    conn = store.conn
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone():
        return {}

    summary = "data.summary_json" if table == PROGRAM_INFO_TABLE else "NULL"
    return {
        id_: Resource(payload, fetch_id, fetched_at, index)
        for id_, payload, fetch_id, fetched_at, index in conn.execute(
            f"SELECT data.{id_column}, data.payload_json, fetch.id, fetch.fetched_at, "
            f"{summary} FROM {table} data JOIN upstream_fetches fetch "
            "ON fetch.id = data.fetch_id"
        )
    }


async def _load_modules(
    session: RequestSession,
    programs: dict[int, Resource],
    cached: dict[int, Resource],
    cutoff: datetime,
) -> tuple[dict[int, Resource], dict[int, dict]]:
    pending: set[int] = set()
    for resource in programs.values():
        pending.update(_references(json.loads(resource.payload_json)["moduleTree"]))
    modules: dict[int, Resource] = {}
    payloads: dict[int, dict] = {}
    while pending:
        reusable = {
            id_: cached[id_]
            for id_ in pending
            if id_ in cached and cached[id_].is_fresh(cutoff)
        }
        batch = reusable | await _fetch_resources(
            session, COURSE_MODULE_URL, pending - reusable.keys()
        )
        modules.update(batch)
        pending = set()
        for id_, resource in batch.items():
            payload = json.loads(resource.payload_json)
            payloads[id_] = payload
            pending.update(_references([payload]))
        pending.difference_update(modules)
    return modules, payloads


def _memberships(
    nodes: list,
    public_modules: dict[int, dict],
    path: tuple[int, ...] = (),
    public_module_id: int | None = None,
    followed: frozenset[int] = frozenset(),
) -> Iterator[tuple]:
    for node in nodes:
        _, module_id, public_id, children, courses = _node_fields(node)
        module_path = (*path, module_id)
        path_json = _json(module_path)
        for course_position, item in enumerate(courses):
            item = _object(item, "module course membership")
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
                module_id,
                public_module_id,
                path_json,
                course_position,
                item.get("compulsory"),
                _json(item.get("terms")),
            )
        yield from _memberships(
            children, public_modules, module_path, public_module_id, followed
        )
        if (
            public_id is not None
            and public_id not in module_path
            and public_id not in followed
        ):
            yield from _memberships(
                [public_modules[public_id]],
                public_modules,
                module_path,
                public_id,
                followed | {public_id},
            )


def _create_tables(store: SQLiteModelStore) -> None:
    # Individual statements keep SQLite from committing an open savepoint.
    statements = (
        f"""CREATE TABLE IF NOT EXISTS {PROGRAM_TREE_TABLE} (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            fetch_id INTEGER NOT NULL REFERENCES upstream_fetches(id),
            payload_json TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS {PROGRAM_INFO_TABLE} (
            program_id INTEGER PRIMARY KEY,
            fetch_id INTEGER NOT NULL REFERENCES upstream_fetches(id),
            department_id INTEGER NOT NULL,
            major_id INTEGER NOT NULL,
            grade TEXT NOT NULL,
            train_type TEXT,
            summary_json TEXT NOT NULL,
            payload_json TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS {COURSE_MODULE_TABLE} (
            module_id INTEGER PRIMARY KEY,
            fetch_id INTEGER NOT NULL REFERENCES upstream_fetches(id),
            payload_json TEXT NOT NULL
        )""",
        f"""CREATE TABLE IF NOT EXISTS {PROGRAM_COURSES_TABLE} (
            program_id INTEGER NOT NULL REFERENCES {PROGRAM_INFO_TABLE}(program_id),
            position INTEGER NOT NULL,
            course_code TEXT NOT NULL,
            course_id INTEGER,
            module_id INTEGER NOT NULL,
            public_module_id INTEGER REFERENCES {COURSE_MODULE_TABLE}(module_id),
            module_path_json TEXT NOT NULL,
            course_position INTEGER NOT NULL,
            compulsory INTEGER,
            terms_json TEXT NOT NULL,
            PRIMARY KEY (program_id, position)
        )""",
        f"""CREATE INDEX IF NOT EXISTS {PROGRAM_COURSES_TABLE}_code
            ON {PROGRAM_COURSES_TABLE}(course_code, program_id)""",
    )
    for statement in statements:
        store.conn.execute(statement)


def _persist(
    store: SQLiteModelStore,
    tree: object,
    index: dict[int, Program],
    programs: dict[int, Resource],
    modules: dict[int, Resource],
    module_payloads: dict[int, dict],
    old_programs: dict[int, Resource],
    old_modules: dict[int, Resource],
) -> None:
    conn = store.conn
    removed_programs = old_programs.keys() - programs.keys()
    removed_modules = old_modules.keys() - modules.keys()
    new_programs = {
        id_: value for id_, value in programs.items() if value.fetch_id is None
    }
    new_modules = {
        id_: value for id_, value in modules.items() if value.fetch_id is None
    }
    rebuild_memberships = bool(
        removed_programs or removed_modules or new_programs or new_modules
    )

    conn.execute("SAVEPOINT public_study_plans")
    try:
        _create_tables(store)
        tree_fetch = store.record_fetch(
            source=PROGRAM_TREE_TABLE, method="GET", url=PROGRAM_TREE_URL
        )
        conn.execute(
            f"INSERT OR REPLACE INTO {PROGRAM_TREE_TABLE}(id, fetch_id, payload_json) "
            "VALUES (1, ?, ?)",
            (tree_fetch, _json(tree)),
        )
        if rebuild_memberships:
            conn.execute(f"DELETE FROM {PROGRAM_COURSES_TABLE}")
        conn.executemany(
            f"DELETE FROM {PROGRAM_INFO_TABLE} WHERE program_id = ?",
            ((id_,) for id_ in removed_programs),
        )
        conn.executemany(
            f"DELETE FROM {COURSE_MODULE_TABLE} WHERE module_id = ?",
            ((id_,) for id_ in removed_modules),
        )
        for id_, resource in new_programs.items():
            program = index[id_]
            fetch = store.record_fetch(
                source=PROGRAM_INFO_TABLE,
                method="GET",
                url=f"{PROGRAM_INFO_URL}/{id_}",
            )
            conn.execute(
                f"""INSERT OR REPLACE INTO {PROGRAM_INFO_TABLE} (
                    program_id, fetch_id, department_id, major_id, grade,
                    train_type, summary_json, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    id_,
                    fetch,
                    program.department_id,
                    program.major_id,
                    program.grade,
                    program.train_type,
                    program.summary_json,
                    resource.payload_json,
                ),
            )
        for id_, resource in new_modules.items():
            fetch = store.record_fetch(
                source=COURSE_MODULE_TABLE,
                method="GET",
                url=f"{COURSE_MODULE_URL}/{id_}",
            )
            conn.execute(
                f"INSERT OR REPLACE INTO {COURSE_MODULE_TABLE}"
                "(module_id, fetch_id, payload_json) VALUES (?, ?, ?)",
                (id_, fetch, resource.payload_json),
            )
        if rebuild_memberships:
            for id_, resource in programs.items():
                memberships = _memberships(
                    json.loads(resource.payload_json)["moduleTree"], module_payloads
                )
                conn.executemany(
                    f"""INSERT INTO {PROGRAM_COURSES_TABLE} (
                        program_id, position, course_code, course_id, module_id,
                        public_module_id, module_path_json, course_position,
                        compulsory, terms_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    ((id_, position, *row) for position, row in enumerate(memberships)),
                )
        for table in (PROGRAM_TREE_TABLE, PROGRAM_INFO_TABLE, COURSE_MODULE_TABLE):
            conn.execute(
                "DELETE FROM upstream_fetches WHERE source = ? "
                f"AND id NOT IN (SELECT fetch_id FROM {table})",
                (table,),
            )
        store.put_metadata(
            {
                f"{PROGRAM_INFO_TABLE}_count": len(programs),
                f"{COURSE_MODULE_TABLE}_count": len(modules),
                f"{PROGRAM_COURSES_TABLE}_count": conn.execute(
                    f"SELECT COUNT(*) FROM {PROGRAM_COURSES_TABLE}"
                ).fetchone()[0],
            }
        )
    except BaseException:
        conn.execute("ROLLBACK TO public_study_plans")
        raise
    finally:
        conn.execute("RELEASE public_study_plans")


async def refresh_study_plans(session: RequestSession, store: SQLiteModelStore) -> None:
    """Refresh the public program index, plan details and reachable public modules."""
    cutoff = datetime.now(UTC) - REFRESH_AGE
    tree = await _fetch(session, PROGRAM_TREE_URL)
    index = _index(tree)
    old_programs = _cached(store, PROGRAM_INFO_TABLE, "program_id")
    old_modules = _cached(store, COURSE_MODULE_TABLE, "module_id")
    if old_programs.keys() - index.keys():
        confirmed = _index(await _fetch(session, PROGRAM_TREE_URL))
        if confirmed != index:
            raise ValueError("A smaller public program index could not be confirmed")
    programs = {
        id_: previous
        for id_, previous in old_programs.items()
        if id_ in index
        and previous.summary_json == index[id_].summary_json
        and previous.is_fresh(cutoff)
    }
    LOGGER.info("Reading %d public plans (%d cached)", len(index), len(programs))
    programs.update(
        await _fetch_resources(
            session, PROGRAM_INFO_URL, index.keys() - programs.keys()
        )
    )
    modules, module_payloads = await _load_modules(
        session, programs, old_modules, cutoff
    )
    _persist(
        store,
        tree,
        index,
        programs,
        modules,
        module_payloads,
        old_programs,
        old_modules,
    )
    LOGGER.info(
        "Stored %d public study plans and %d public modules",
        len(programs),
        len(modules),
    )
