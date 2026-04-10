import argparse
import asyncio
import logging
import os
import shutil
from pathlib import Path

from src import make_curriculum, make_rss
from tools.sqlite_snapshot import export_sqlite_snapshot

CurriculumMode = "all", "window"


def resolve_curriculum_mode(explicit_mode: str | None) -> str:
    if explicit_mode:
        return explicit_mode

    env_mode = os.getenv("LIFE_USTC_CURRICULUM_MODE")
    if env_mode in CurriculumMode:
        return env_mode

    if os.getenv("CI", "").strip().lower() == "true":
        return "window"

    return "all"


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
        "--curriculum-semester-mode",
        choices=CurriculumMode,
        help="Choose whether to refresh all semesters or only a time window",
    )
    parser.add_argument(
        "--curriculum-window-years",
        type=int,
        default=int(os.getenv("LIFE_USTC_CURRICULUM_WINDOW_YEARS", "1")),
        help="Year window on each side of today when using window mode",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    base_dir = Path(__file__).resolve().parent
    build_dir = base_dir / "build"
    build_dir.mkdir(exist_ok=True)

    static_dir = base_dir / "static"
    shutil.copytree(static_dir, build_dir, dirs_exist_ok=True)

    run_all = not args.rss and not args.curriculum
    run_rss = args.rss or run_all
    run_curriculum = args.curriculum or run_all
    curriculum_mode = resolve_curriculum_mode(args.curriculum_semester_mode)

    tasks = []
    if run_rss:
        tasks.append(make_rss())
    if run_curriculum:
        tasks.append(
            make_curriculum(
                mode=curriculum_mode,
                window_years=args.curriculum_window_years,
            )
        )

    async def _run():
        await asyncio.gather(*tasks)

    asyncio.run(_run())
    snapshot_path = export_sqlite_snapshot(build_dir)
    logging.info("Exported SQLite snapshot to %s", snapshot_path)


if __name__ == "__main__":
    main()
