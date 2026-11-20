from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
from typing import cast

EXPECTED_SCHEMA_COUNT = 11
OBSERVED_SCHEMA_COUNT = 5
CATALOG_OUTPUTS = {
    "course_catalog": ("course_search", "course_infos"),
    "course_substitutions": ("course_substitute_pool_list",),
    "section_catalog": (),  # Per-semester coverage permits empty terms.
    "catalog_groups": (
        "course_group_entries",
        "course_group_endpoints",
        "course_groups",
        "course_group_members",
    ),
    "study_plans": (
        "program_tree",
        "program_info",
        "course_module_info",
        "program_courses",
    ),
}
BUILDER_NAMES = ("curriculum", *CATALOG_OUTPUTS, "young", "rss", "blackboard")


class BuildVerificationError(RuntimeError):
    pass


def _load_builders(status_path: Path) -> dict[str, dict[str, str]]:
    try:
        status = json.loads(status_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BuildVerificationError(
            f"Unable to read build status from {status_path}: {error}"
        ) from error

    if not isinstance(status, dict) or not isinstance(status.get("builders"), dict):
        raise BuildVerificationError(
            f"Build status from {status_path} does not contain a builders object"
        )

    builders = cast(dict[str, dict[str, str]], status["builders"])
    missing = set(BUILDER_NAMES) - builders.keys()
    if missing:
        raise BuildVerificationError(
            f"Build status is missing builder(s): {', '.join(sorted(missing))}"
        )
    invalid = [
        name
        for name in BUILDER_NAMES
        if not isinstance(builders[name], dict)
        or builders[name].get("status") not in {"ok", "failed"}
    ]
    if invalid:
        raise BuildVerificationError(
            f"Build status has invalid result(s): {', '.join(invalid)}"
        )
    return builders


def _append_summary(path: Path | None, content: str) -> None:
    if path is not None:
        with path.open("a", encoding="utf-8") as output:
            output.write(content)


def _write_status_summary(
    summary_path: Path | None,
    builders: dict[str, dict[str, str]],
) -> None:
    lines = ["## Static build status\n\n", "| Builder | Status |\n", "|---|---|\n"]
    lines.extend(f"| {name} | {builders[name]['status']} |\n" for name in BUILDER_NAMES)
    if builders["curriculum"]["status"] != "ok":
        lines.extend(
            [
                "\n",
                "Contract diagnostics are skipped because the curriculum builder "
                "did not complete successfully.\n",
            ]
        )
    _append_summary(summary_path, "".join(lines) + "\n")


def _verify_expected_schemas(build_dir: Path) -> None:
    schema_dir = build_dir / "schemas" / "upstream"
    if not schema_dir.is_dir():
        raise BuildVerificationError(f"Missing upstream schema directory: {schema_dir}")
    schemas = list(schema_dir.rglob("*.expected.schema.json"))
    if len(schemas) != EXPECTED_SCHEMA_COUNT:
        raise BuildVerificationError(
            f"Expected {EXPECTED_SCHEMA_COUNT} upstream schemas, found {len(schemas)}"
        )


def _verify_builder_outputs(
    build_dir: Path,
    builders: dict[str, dict[str, str]],
) -> None:
    successful = {name for name, result in builders.items() if result["status"] == "ok"}
    snapshot = (build_dir / "life-ustc-static.sqlite").resolve()
    if successful - {"rss"} and not snapshot.is_file():
        raise BuildVerificationError(f"Missing SQLite output: {snapshot}")
    if (
        "curriculum" in successful
        and not (build_dir / "life-ustc-static-guesses.sqlite").is_file()
    ):
        raise BuildVerificationError("Missing curriculum guesses snapshot")
    catalog = successful.intersection(CATALOG_OUTPUTS)
    if catalog:
        try:
            with closing(
                sqlite3.connect(f"{snapshot.as_uri()}?mode=ro", uri=True)
            ) as db:
                for name in catalog:
                    for suffix in CATALOG_OUTPUTS[name]:
                        table = f"catalog_teach_{suffix}"
                        if not db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                            raise BuildVerificationError(
                                f"Empty school catalog table: {table}"
                            )
                if "section_catalog" in catalog:
                    _verify_section_counts(db)
        except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
            raise BuildVerificationError(
                f"Unreadable school catalog: {error}"
            ) from error
    if "rss" in successful:
        feeds = list((build_dir / "rss").glob("*.xml"))
        if not feeds or any(feed.stat().st_size == 0 for feed in feeds):
            raise BuildVerificationError("RSS builder reported success without feeds")


def _verify_section_counts(db: sqlite3.Connection) -> None:
    terms = db.execute(
        "SELECT key, value FROM metadata "
        "WHERE key GLOB 'catalog_semester_details_[0-9]*'"
    )
    declared = {
        key.rsplit("_", 1)[1]: json.loads(value)["lesson_profile_rows"]
        for key, value in terms
    }
    if not declared:
        raise BuildVerificationError("Missing section coverage")
    actual = {}
    if db.execute(
        "SELECT 1 FROM pragma_table_info('catalog_teach_lesson_infos') "
        "WHERE name='semester_id'"
    ).fetchone():
        actual = dict(
            db.execute(
                "SELECT semester_id, count(*) FROM catalog_teach_lesson_infos "
                "GROUP BY semester_id"
            )
        )
    if actual != {term: count for term, count in declared.items() if count}:
        raise BuildVerificationError(
            "Section counts differ from declared semester coverage"
        )


def _verify_room_maps(build_dir: Path) -> None:
    manifest_path = build_dir / "room_maps.json"
    if not manifest_path.is_file():
        raise BuildVerificationError(f"Missing room-map manifest: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BuildVerificationError(
            f"Unable to parse room-map manifest {manifest_path}: {error}"
        ) from error

    rooms = manifest.get("rooms") if isinstance(manifest, dict) else None
    if not isinstance(rooms, list) or not rooms:
        raise BuildVerificationError("Room-map manifest has no rooms")
    codes: set[str] = set()
    for room in rooms:
        if not isinstance(room, dict):
            raise BuildVerificationError("Room-map manifest contains a non-object room")
        code = room.get("code")
        image_path_value = room.get("imagePath")
        if not isinstance(code, str) or not code or code in codes:
            raise BuildVerificationError(
                f"Room-map manifest has invalid code: {code!r}"
            )
        if not isinstance(image_path_value, str) or not image_path_value:
            raise BuildVerificationError(f"Room-map {code} has no image path")
        image_path = Path(image_path_value)
        if image_path.is_absolute() or ".." in image_path.parts:
            raise BuildVerificationError(f"Room-map {code} has unsafe image path")
        image_file = build_dir / image_path
        if not image_file.is_file() or image_file.stat().st_size == 0:
            raise BuildVerificationError(
                f"Missing room-map image for {code}: {image_file}"
            )
        codes.add(code)


def _verify_contract_report(
    diagnostic_dir: Path,
    summary_path: Path | None,
) -> None:
    observed_schemas = list(diagnostic_dir.rglob("*.observed.schema.json"))
    if len(observed_schemas) != OBSERVED_SCHEMA_COUNT:
        raise BuildVerificationError(
            f"Expected {OBSERVED_SCHEMA_COUNT} observed schemas, "
            f"found {len(observed_schemas)}"
        )
    report_path = diagnostic_dir / "contract-report.json"
    if not report_path.is_file() or report_path.stat().st_size == 0:
        raise BuildVerificationError(f"Missing contract report: {report_path}")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BuildVerificationError(
            f"Unable to parse contract report {report_path}: {error}"
        ) from error

    try:
        report_summary = report["summary"]
        errors = report_summary["errors"]
        warnings = report_summary["warnings"]
        issue_counts = report_summary["issueCounts"]
        coverage = report["coverage"]
    except (KeyError, TypeError) as error:
        raise BuildVerificationError(
            f"Contract report {report_path} has an invalid shape"
        ) from error

    print("Upstream contract diagnostics:", json.dumps(report_summary, sort_keys=True))
    lines = [
        "## Upstream contract diagnostics\n\n",
        f"- Errors: {errors}\n",
        f"- Warnings: {warnings}\n",
        f"- Counts: `{json.dumps(issue_counts, sort_keys=True)}`\n",
        "\n| Endpoint | Coverage | Fetches | Missing contexts |\n",
        "|---|---:|---:|---:|\n",
    ]
    for endpoint, endpoint_coverage in coverage.items():
        lines.append(
            f"| {endpoint} | {endpoint_coverage['coverageComplete']} | "
            f"{endpoint_coverage['independentFetchCount']} | "
            f"{len(endpoint_coverage['missingContexts'])} |\n"
        )
    _append_summary(summary_path, "".join(lines) + "\n")


def verify_build(
    *,
    build_dir: Path,
    status_path: Path,
    diagnostic_dir: Path,
    summary_path: Path | None = None,
) -> None:
    """Verify a build, handling builder status before diagnostics."""
    builders = _load_builders(status_path)
    _write_status_summary(summary_path, builders)
    _verify_expected_schemas(build_dir)
    _verify_builder_outputs(build_dir, builders)
    _verify_room_maps(build_dir)

    if builders["curriculum"]["status"] == "ok":
        _verify_contract_report(diagnostic_dir, summary_path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify static build outputs")
    parser.add_argument("--build-dir", type=Path, default=Path("build"))
    parser.add_argument(
        "--status-path", type=Path, default=Path("build/build-status.json")
    )
    parser.add_argument(
        "--diagnostic-dir",
        type=Path,
        default=Path(".artifacts/upstream-contracts"),
    )
    args = parser.parse_args()
    summary_path = (
        Path(os.environ["GITHUB_STEP_SUMMARY"])
        if os.environ.get("GITHUB_STEP_SUMMARY")
        else None
    )
    try:
        verify_build(
            build_dir=args.build_dir,
            status_path=args.status_path,
            diagnostic_dir=args.diagnostic_dir,
            summary_path=summary_path,
        )
    except BuildVerificationError as error:
        print(f"::error title=Build verification failed::{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
