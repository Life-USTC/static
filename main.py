from pathlib import Path
import shutil
import asyncio
import argparse
import logging

from src import make_curriculum, make_rss


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

    tasks = []
    if run_rss:
        tasks.append(make_rss())
    if run_curriculum:
        tasks.append(make_curriculum())

    async def _run():
        await asyncio.gather(*tasks)

    asyncio.run(_run())


if __name__ == "__main__":
    main()
