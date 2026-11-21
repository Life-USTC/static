import argparse
import asyncio
import json
import logging
import os
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, closing
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import httpx
from dotenv import load_dotenv
from rich.logging import RichHandler

from src.http import RequestSession
from src.platforms.bb import make_blackboard
from src.platforms.catalog.courses import refresh_course_catalog
from src.platforms.catalog.groups import refresh_catalog_groups
from src.platforms.catalog.plans import refresh_study_plans
from src.platforms.catalog.sections import refresh_catalog_semester_details
from src.platforms.catalog.semesters import make_curriculum
from src.platforms.catalog.substitutions import refresh_course_substitutions
from src.platforms.passport import USTCSession
from src.platforms.young import make_young_events
from src.sqlite_store import GUESSES_FILENAME, SNAPSHOT_FILENAME, SQLiteModelStore
from tools.room_maps import build_room_maps
from tools.upstream_schemas import export_upstream_schemas


def _copy_output(source: Path, destination: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, destination)
    elif source.exists():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _restore_outputs(output_paths: tuple[Path, ...], backup_dir: Path) -> None:
    for index, output_path in enumerate(output_paths):
        if output_path.is_dir():
            shutil.rmtree(output_path)
        elif output_path.exists():
            output_path.unlink()
        _copy_output(backup_dir / str(index), output_path)


async def _run_builders(
    builders: list[tuple[str, Callable[[], Awaitable[None]], tuple[Path, ...]]],
    *,
    status_path: Path,
) -> dict[str, dict[str, str]]:
    results: dict[str, dict[str, str]] = {}
    for name, builder, output_paths in builders:
        with tempfile.TemporaryDirectory(prefix=f"static-{name}-") as temporary_dir:
            backup_dir = Path(temporary_dir)
            for index, output_path in enumerate(output_paths):
                _copy_output(output_path, backup_dir / str(index))

            grouped = bool(os.getenv("GITHUB_ACTIONS"))
            if grouped:
                print(f"::group::{name}", flush=True)
            started = time.monotonic()
            logging.info("Starting %s", name)
            try:
                await builder()
            except Exception as error:
                _restore_outputs(output_paths, backup_dir)
                logging.exception("%s builder failed", name)
                if grouped:
                    print(
                        f"::error title={name} builder failed::{type(error).__name__}"
                    )
                results[name] = {"status": "failed", "error": type(error).__name__}
            else:
                results[name] = {"status": "ok"}
            finally:
                logging.info("%s finished in %.1fs", name, time.monotonic() - started)
                if grouped:
                    print("::endgroup::", flush=True)

    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "builders": results,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return results


async def _main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description="Life-USTC static builders")
    for flag, help_text in {
        "curriculum": "Generate curriculum data",
        "young": "Generate Young event data",
        "blackboard": "Crawl public Blackboard course material",
        "verify-upstream-contract": "Refresh curriculum and verify its contracts",
        "course-catalog": "Refresh all public course data",
        "catalog-relationships": "Refresh course groups and study plans",
    }.items():
        parser.add_argument("--" + flag, action="store_true", help=help_text)
    parser.add_argument(
        "--section-semester",
        type=int,
        action="append",
        metavar="ID",
        help="Refresh class profiles for this semester (repeatable)",
    )
    parser.add_argument("--platform", choices=("academic", "young", "blackboard"))
    args = parser.parse_args()
    if args.platform:
        args.curriculum = args.course_catalog = args.platform == "academic"
        args.young = args.platform == "young"
        args.blackboard = args.platform == "blackboard"

    logging.basicConfig(
        level=logging.INFO,
        format="%(message)s",
        handlers=[
            RichHandler(
                show_path=False,
                markup=False,
                rich_tracebacks=True,
                tracebacks_show_locals=False,
                tracebacks_max_frames=8,
            )
        ],
        force=True,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    base_dir = Path(__file__).resolve().parent
    build_dir = base_dir / "build"
    build_dir.mkdir(exist_ok=True)

    run_all = not any(vars(args).values())
    catalog = run_all or args.course_catalog
    sections = catalog or bool(args.section_semester)
    relationships = catalog or args.catalog_relationships
    run_curriculum = run_all or args.curriculum or args.verify_upstream_contract
    catalog_only = not (run_curriculum or args.young or args.blackboard)
    if not args.platform and not catalog_only:
        static_dir = base_dir / "static"
        shutil.copytree(static_dir, build_dir, dirs_exist_ok=True)
        build_room_maps(static_dir, build_dir)

    resources = AsyncExitStack()
    session_task = None

    authenticated = bool(
        os.getenv("USTC_PASSPORT_USERNAME") and os.getenv("USTC_PASSPORT_PASSWORD")
    )

    async def connect():
        if authenticated:
            return await resources.enter_async_context(USTCSession())
        client = await resources.enter_async_context(
            httpx.AsyncClient(follow_redirects=True)
        )
        return RequestSession(client=client, page=None)

    async def school_session():
        nonlocal session_task
        if session_task is None:
            session_task = asyncio.create_task(connect())
        return await session_task

    async def run_curriculum_builder():
        await make_curriculum(
            verify_upstream_contract=args.verify_upstream_contract,
            session=await school_session() if authenticated else None,
        )

    async def run_catalog(refresh):
        session = await school_session()
        database = SQLiteModelStore(build_dir / SNAPSHOT_FILENAME, reset=False)
        with closing(database), database.conn:
            await refresh(session, database)

    builders: list[tuple[str, Callable[[], Awaitable[None]], tuple[Path, ...]]] = []
    if run_curriculum:
        builders.append(
            (
                "curriculum",
                run_curriculum_builder,
                (
                    build_dir / SNAPSHOT_FILENAME,
                    build_dir / GUESSES_FILENAME,
                    build_dir / "schemas" / "upstream",
                    base_dir / ".artifacts" / "upstream-contracts",
                ),
            )
        )
    for enabled, name, refresh in (
        (catalog, "course_catalog", refresh_course_catalog),
        (catalog, "course_substitutions", refresh_course_substitutions),
        (
            sections,
            "section_catalog",
            partial(
                refresh_catalog_semester_details, semester_ids=args.section_semester
            ),
        ),
        (
            relationships,
            "catalog_groups",
            refresh_catalog_groups,
        ),
        (relationships, "study_plans", refresh_study_plans),
    ):
        if enabled:
            # Each public collector commits its own validated source or semester.
            builders.append((name, partial(run_catalog, refresh), ()))
    for name, refresh in (
        ("young", make_young_events),
        ("blackboard", make_blackboard),
    ):
        if run_all or getattr(args, name):
            builders.append((name, refresh, (build_dir / SNAPSHOT_FILENAME,)))

    async with resources:
        results = await _run_builders(
            builders, status_path=build_dir / "build-status.json"
        )
    if catalog_only and any(
        result["status"] == "failed" for result in results.values()
    ):
        raise RuntimeError("Public course refresh incomplete; see build-status.json")
    if results and all(result["status"] == "failed" for result in results.values()):
        raise RuntimeError("All selected static builders failed")

    curriculum_succeeded = results.get("curriculum", {}).get("status") == "ok"
    if not curriculum_succeeded and (args.young or sections or relationships):
        schema_paths = export_upstream_schemas(build_dir / "schemas" / "upstream")
        logging.info("Exported %s upstream JSON schemas", len(schema_paths))


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
