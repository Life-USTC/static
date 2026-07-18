import argparse
import asyncio
import json
import logging
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

from src import make_curriculum, make_rss, make_young_events
from src.sqlite_store import GUESSES_FILENAME, SNAPSHOT_FILENAME
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
                logging.exception("%s builder failed; restored previous output", name)
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

    builders: list[tuple[str, Callable[[], Awaitable[None]], tuple[Path, ...]]] = []
    if run_rss:
        builders.append(("rss", make_rss, (build_dir / "rss",)))
    if run_curriculum:
        builders.append(
            (
                "curriculum",
                make_curriculum,
                (
                    build_dir / SNAPSHOT_FILENAME,
                    build_dir / GUESSES_FILENAME,
                ),
            )
        )
    if run_young:
        builders.append(("young", make_young_events, (build_dir / SNAPSHOT_FILENAME,)))

    results = asyncio.run(
        _run_builders(builders, status_path=build_dir / "build-status.json")
    )
    if results and all(result["status"] == "failed" for result in results.values()):
        raise RuntimeError("All selected static builders failed")

    if run_curriculum or run_young:
        schema_paths = export_upstream_schemas(build_dir / "schemas" / "upstream")
        logging.info("Exported %s upstream JSON schemas", len(schema_paths))


if __name__ == "__main__":
    main()
