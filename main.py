import argparse
import asyncio
import json
import logging
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, closing
from datetime import UTC, datetime
from functools import partial
from pathlib import Path

import httpx

from src import make_blackboard, make_curriculum, make_rss, make_young_events
from src.catalog_groups import refresh_catalog_groups
from src.catalog_semester_details import refresh_catalog_semester_details
from src.course_catalog import refresh_course_catalog
from src.course_substitutions import refresh_course_substitutions
from src.sqlite_store import GUESSES_FILENAME, SNAPSHOT_FILENAME, SQLiteModelStore
from src.study_plans import refresh_study_plans
from src.utils.auth import RequestSession, USTCSession
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

            try:
                await builder()
            except Exception as error:
                _restore_outputs(output_paths, backup_dir)
                logging.exception("%s builder failed", name)
                print(
                    f"::error title={name} builder failed::"
                    f"{type(error).__name__}: {error}"
                )
                results[name] = {"status": "failed", "error": type(error).__name__}
            else:
                results[name] = {"status": "ok"}

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


async def main() -> None:
    parser = argparse.ArgumentParser(description="Life-USTC static builders")
    for flag, help_text in {
        "rss": "Generate RSS",
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
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    base_dir = Path(__file__).resolve().parent
    build_dir = base_dir / "build"
    build_dir.mkdir(exist_ok=True)

    run_all = not any(vars(args).values())
    catalog = run_all or args.course_catalog
    sections = catalog or bool(args.section_semester)
    relationships = catalog or args.catalog_relationships
    run_curriculum = run_all or args.curriculum or args.verify_upstream_contract
    catalog_only = not (run_curriculum or args.rss or args.young or args.blackboard)
    if not catalog_only:
        static_dir = base_dir / "static"
        shutil.copytree(static_dir, build_dir, dirs_exist_ok=True)
        build_room_maps(static_dir, build_dir)

    builders: list[tuple[str, Callable[[], Awaitable[None]], tuple[Path, ...]]] = []
    if run_all or args.rss:
        builders.append(("rss", make_rss, (build_dir / "rss",)))
    if run_curriculum:
        builders.append(
            (
                "curriculum",
                partial(
                    make_curriculum,
                    verify_upstream_contract=args.verify_upstream_contract,
                ),
                (
                    build_dir / SNAPSHOT_FILENAME,
                    build_dir / GUESSES_FILENAME,
                    build_dir / "schemas" / "upstream",
                    base_dir / ".artifacts" / "upstream-contracts",
                ),
            )
        )
    resources = AsyncExitStack()
    catalog_session = None

    async def run_catalog(refresh) -> None:
        nonlocal catalog_session
        if catalog_session is None:
            login = USTCSession()
            if login.username and login.password:
                catalog_session = await resources.enter_async_context(login)
            else:
                client = await resources.enter_async_context(
                    httpx.AsyncClient(follow_redirects=True)
                )
                catalog_session = RequestSession(client=client, page=None)
        database = SQLiteModelStore(build_dir / SNAPSHOT_FILENAME, reset=False)
        with closing(database), database.conn:
            await refresh(catalog_session, database)

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


if __name__ == "__main__":
    asyncio.run(main())
