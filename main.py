import argparse
import asyncio
import json
import logging
import shutil
import tempfile
from collections.abc import Awaitable, Callable
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
from src.utils.auth import RequestSession
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


async def run_public_course_builder(
    build_dir: Path,
    refresh: Callable[[RequestSession, SQLiteModelStore], Awaitable[None]],
) -> None:
    store = SQLiteModelStore(build_dir / SNAPSHOT_FILENAME, reset=False)
    try:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            await refresh(RequestSession(client=client, page=None), store)
    except BaseException:
        store.conn.rollback()
        raise
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Life-USTC static builders")
    parser.add_argument(
        "--rss",
        action="store_true",
        help="Generate RSS",
    )
    parser.add_argument(
        "--curriculum",
        action="store_true",
        help="Generate curriculum data",
    )
    parser.add_argument(
        "--course-catalog",
        action="store_true",
        help="Refresh public course, section, catalog group, and study plan data",
    )
    parser.add_argument(
        "--catalog-relationships",
        action="store_true",
        help="Refresh public course groups and study plans only",
    )
    parser.add_argument(
        "--section-semester",
        type=int,
        action="append",
        metavar="ID",
        help="Refresh public section profiles for a specific semester (repeatable)",
    )
    parser.add_argument(
        "--young",
        action="store_true",
        help="Generate Young event data",
    )
    parser.add_argument(
        "--blackboard",
        action="store_true",
        help="Crawl public Blackboard course material through a guest session",
    )
    parser.add_argument(
        "--verify-upstream-contract",
        action="store_true",
        help="Fully refresh curriculum sources and verify upstream contracts",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    base_dir = Path(__file__).resolve().parent
    build_dir = base_dir / "build"
    build_dir.mkdir(exist_ok=True)

    other_builders_selected = any(
        (
            args.rss,
            args.curriculum,
            args.young,
            args.blackboard,
            args.verify_upstream_contract,
        )
    )
    sections_selected = args.course_catalog or bool(args.section_semester)
    public_selected = sections_selected or args.catalog_relationships
    catalog_only = public_selected and not other_builders_selected
    if not catalog_only:
        static_dir = base_dir / "static"
        shutil.copytree(static_dir, build_dir, dirs_exist_ok=True)
        build_room_maps(static_dir, build_dir)

    run_all = not (public_selected or other_builders_selected)
    run_rss = args.rss or run_all
    run_curriculum = args.curriculum or args.verify_upstream_contract or run_all
    run_young = args.young or run_all
    run_blackboard = args.blackboard or run_all

    builders: list[tuple[str, Callable[[], Awaitable[None]], tuple[Path, ...]]] = []
    if run_rss:
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
    if args.course_catalog or run_all:
        for name, refresh in (
            ("course_catalog", refresh_course_catalog),
            ("course_substitutions", refresh_course_substitutions),
        ):
            builders.append(
                (
                    name,
                    partial(run_public_course_builder, build_dir, refresh),
                    (build_dir / SNAPSHOT_FILENAME,),
                )
            )
    if sections_selected or run_all:
        # This source commits each validated semester independently. A failed
        # historical request should not roll back another semester's refresh.
        builders.append(
            (
                "section_catalog",
                partial(
                    run_public_course_builder,
                    build_dir,
                    partial(
                        refresh_catalog_semester_details,
                        semester_ids=args.section_semester,
                    ),
                ),
                (),
            )
        )
    if args.course_catalog or args.catalog_relationships or run_all:
        for name, refresh in (
            ("catalog_groups", refresh_catalog_groups),
            ("study_plans", refresh_study_plans),
        ):
            builders.append(
                (name, partial(run_public_course_builder, build_dir, refresh), ()),
            )
    if run_young:
        builders.append(("young", make_young_events, (build_dir / SNAPSHOT_FILENAME,)))
    if run_blackboard:
        builders.append(
            (
                "blackboard",
                make_blackboard,
                (build_dir / SNAPSHOT_FILENAME,),
            )
        )

    results = asyncio.run(
        _run_builders(builders, status_path=build_dir / "build-status.json")
    )
    if catalog_only and any(
        result["status"] == "failed" for result in results.values()
    ):
        raise RuntimeError("Public course refresh incomplete; see build-status.json")
    if results and all(result["status"] == "failed" for result in results.values()):
        raise RuntimeError("All selected static builders failed")

    curriculum_succeeded = (
        run_curriculum and results.get("curriculum", {}).get("status") == "ok"
    )
    if not curriculum_succeeded and (run_young or public_selected or run_all):
        schema_paths = export_upstream_schemas(build_dir / "schemas" / "upstream")
        logging.info("Exported %s upstream JSON schemas", len(schema_paths))


if __name__ == "__main__":
    main()
