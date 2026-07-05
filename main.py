import argparse
import asyncio
import logging
import shutil
from pathlib import Path

from src import make_curriculum, make_rss, make_young_events
from tools.upstream_schemas import export_upstream_schemas


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
        "--young",
        action="store_true",
        help="Generate Young event data",
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

    run_all = not args.rss and not args.curriculum and not args.young
    run_rss = args.rss or run_all
    run_curriculum = args.curriculum or run_all
    run_young = args.young or run_all

    async def _run():
        if run_rss:
            await make_rss()
        if run_curriculum:
            await make_curriculum()
        if run_young:
            await make_young_events()

    asyncio.run(_run())
    if run_curriculum or run_young:
        schema_paths = export_upstream_schemas(build_dir / "schemas" / "upstream")
        logging.info("Exported %s upstream JSON schemas", len(schema_paths))


if __name__ == "__main__":
    main()
