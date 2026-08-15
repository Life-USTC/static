from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .observed_schema import JsonSchema, JsonValue, ObservedNode
from .upstream_contract_comparison import (
    ContractIssue,
    UpstreamContractError,
    compare_observed_to_expected,
    expected_schema,
)
from .upstream_contracts import (
    CURRICULUM_UPSTREAM_RESPONSE_MODELS,
    UPSTREAM_RESPONSE_MODELS,
    UpstreamResponseModel,
)


class ObservedContractCollector:
    """Accumulates raw upstream JSON before Pydantic validation."""

    def __init__(self) -> None:
        self._roots: dict[str, ObservedNode] = {}
        self._fetch_contexts: dict[str, set[str]] = {}
        self._observed_coverage_contexts: dict[str, set[str]] = {}
        self._required_coverage_contexts: dict[str, set[str]] = {}
        self._observation_count = 0

    @property
    def endpoint_names(self) -> set[str]:
        return set(self._roots)

    def require_contexts(self, endpoint: str, contexts: set[str]) -> None:
        self._required_coverage_contexts.setdefault(endpoint, set()).update(contexts)

    def observe(
        self,
        endpoint: str,
        payload: JsonValue,
        *,
        fetch_context: str | None = None,
        coverage_context: str | None = None,
    ) -> None:
        self._observation_count += 1
        fetch_context = fetch_context or f"observation-{self._observation_count}"
        coverage_context = coverage_context or fetch_context
        self._roots.setdefault(endpoint, ObservedNode()).observe(payload)
        self._fetch_contexts.setdefault(endpoint, set()).add(fetch_context)
        self._observed_coverage_contexts.setdefault(endpoint, set()).add(
            coverage_context
        )

    def observe_and_assert_compatible(
        self,
        endpoint: str,
        payload: JsonValue,
        model: UpstreamResponseModel,
        *,
        fetch_context: str,
        coverage_context: str | None = None,
    ) -> None:
        self.observe(
            endpoint,
            payload,
            fetch_context=fetch_context,
            coverage_context=coverage_context,
        )
        self.assert_payload_compatible(endpoint, model)

    def assert_payload_compatible(
        self, endpoint: str, model: UpstreamResponseModel
    ) -> None:
        """Reject inbound drift before Pydantic has a chance to coerce it."""
        issues = compare_observed_to_expected(
            endpoint,
            self._roots[endpoint],
            expected_schema(model),
            check_expected_breadth=False,
        )
        errors = [issue for issue in issues if issue.severity == "error"]
        if errors:
            raise UpstreamContractError(errors)

    def observed_schema(self, endpoint: str) -> JsonSchema:
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            **self._roots[endpoint].to_json_schema(),
        }

    def coverage(self, endpoint: str) -> dict[str, Any]:
        fetch_contexts = self._fetch_contexts.get(endpoint, set())
        observed_contexts = self._observed_coverage_contexts.get(endpoint, set())
        required_contexts = self._required_coverage_contexts.get(endpoint, set())
        missing_contexts = required_contexts - observed_contexts
        coverage_complete = bool(required_contexts) and not missing_contexts
        return {
            "coverageComplete": coverage_complete,
            "independentFetchCount": len(fetch_contexts),
            "missingContexts": sorted(missing_contexts),
            "observedContexts": sorted(observed_contexts),
            "observedFetchContexts": sorted(fetch_contexts),
            "requiredContexts": sorted(required_contexts),
        }

    def verify(
        self,
        models: dict[str, UpstreamResponseModel],
    ) -> list[ContractIssue]:
        issues: list[ContractIssue] = []
        for endpoint, model in models.items():
            root = self._roots.get(endpoint)
            if root is None:
                issues.append(
                    ContractIssue(
                        severity="error",
                        code="ENDPOINT_NOT_OBSERVED",
                        endpoint=endpoint,
                        path="$",
                        message="no successful raw response was observed in this build",
                    )
                )
                continue
            coverage = self.coverage(endpoint)
            issues.extend(
                compare_observed_to_expected(
                    endpoint,
                    root,
                    expected_schema(model),
                    check_expected_breadth=True,
                    optional_mismatch_is_error=(
                        coverage["coverageComplete"]
                        or coverage["independentFetchCount"] >= 2
                    ),
                )
            )
        return sorted(
            issues,
            key=lambda issue: (
                issue.severity != "error",
                issue.endpoint,
                issue.path,
                issue.code,
            ),
        )


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _replace_json_directory(
    output_dir: Path,
    files: Mapping[str, Any],
) -> None:
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent)
    )
    backup_dir = output_dir.with_name(f".{output_dir.name}.previous")
    try:
        for name, value in sorted(files.items()):
            _write_json(temporary_dir / name, value)
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
        if output_dir.exists():
            os.replace(output_dir, backup_dir)
        os.replace(temporary_dir, output_dir)
        if backup_dir.exists():
            shutil.rmtree(backup_dir)
    except Exception:
        if backup_dir.exists() and not output_dir.exists():
            os.replace(backup_dir, output_dir)
        raise
    finally:
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir)


def publish_contract_artifacts(
    collector: ObservedContractCollector,
    expected_output_dir: Path,
    diagnostic_output_dir: Path,
    *,
    observed_models: dict[str, UpstreamResponseModel] = (
        CURRICULUM_UPSTREAM_RESPONSE_MODELS
    ),
    expected_models: dict[str, UpstreamResponseModel] = UPSTREAM_RESPONSE_MODELS,
) -> list[ContractIssue]:
    """Verify first, then atomically replace the complete contract directory."""
    issues = collector.verify(observed_models)
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise UpstreamContractError(issues)

    issue_counts = Counter(issue.code for issue in issues)
    report = {
        "coverage": {
            endpoint: collector.coverage(endpoint)
            for endpoint in sorted(observed_models)
        },
        "issues": [issue.as_dict() for issue in issues],
        "policy": {
            "alwaysNull": "warning",
            "emptyArrayItems": "warning",
            "expectedNullableButNoObservedNull": (
                "warning; one build is insufficient evidence to remove long-term "
                "nullable semantics"
            ),
            "expectedOptionalButAlwaysPresent": {
                "errorWhen": (
                    "at least two object instances and either required fetch-context "
                    "coverage is complete or two independent fetches were observed"
                ),
                "otherwise": "warning",
            },
            "fieldNeverObserved": "warning",
            "incompatibleObservedShape": "error",
            "typeBranchNotExercised": "warning",
            "unobservedEndpoint": "error",
        },
        "summary": {
            "errors": 0,
            "issueCounts": dict(sorted(issue_counts.items())),
            "warnings": sum(issue.severity == "warning" for issue in issues),
        },
    }
    _replace_json_directory(
        diagnostic_output_dir,
        {
            **{
                f"{name}.observed.schema.json": collector.observed_schema(name)
                for name in observed_models
            },
            "contract-report.json": report,
        },
    )
    _replace_json_directory(
        expected_output_dir,
        {
            f"{name}.expected.schema.json": expected_schema(model)
            for name, model in expected_models.items()
        },
    )

    return issues


def log_contract_diagnostics(
    issues: list[ContractIssue],
    report_path: Path,
    *,
    logger: logging.Logger,
    maximum_issue_lines: int = 10,
) -> None:
    warnings = [issue for issue in issues if issue.severity == "warning"]
    counts = Counter(issue.code for issue in warnings)
    if not warnings:
        logger.info("Upstream contract diagnostics passed; report=%s", report_path)
        return
    logger.warning(
        "Upstream contract diagnostics: %s warning(s) by code=%s; report=%s",
        len(warnings),
        dict(sorted(counts.items())),
        report_path,
    )
    for issue in warnings[:maximum_issue_lines]:
        logger.warning(
            "Contract warning %s %s %s: %s",
            issue.code,
            issue.endpoint,
            issue.path,
            issue.message,
        )
    remaining = len(warnings) - maximum_issue_lines
    if remaining > 0:
        logger.warning("%s additional contract warning(s) are in the report", remaining)
