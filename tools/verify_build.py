from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import cast

EXPECTED_SCHEMA_COUNT = 7
OBSERVED_SCHEMA_COUNT = 5
BUILDER_NAMES = ("curriculum", "young", "rss")


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
    failed = [name for name in BUILDER_NAMES if builders[name]["status"] != "ok"]
    lines = ["## Static build status\n\n", "| Builder | Status |\n", "|---|---|\n"]
    lines.extend(f"| {name} | {builders[name]['status']} |\n" for name in BUILDER_NAMES)
    if failed:
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
    if builders["curriculum"]["status"] == "ok":
        for filename in ("life-ustc-static.sqlite", "life-ustc-static-guesses.sqlite"):
            path = build_dir / filename
            if not path.is_file():
                raise BuildVerificationError(f"Missing curriculum output: {path}")
    if builders["young"]["status"] == "ok":
        path = build_dir / "life-ustc-static.sqlite"
        if not path.is_file():
            raise BuildVerificationError(f"Missing Young output: {path}")
    if builders["rss"]["status"] == "ok":
        feeds = list((build_dir / "rss").glob("*.xml"))
        if not feeds or any(feed.stat().st_size == 0 for feed in feeds):
            raise BuildVerificationError("RSS builder reported success without feeds")


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
