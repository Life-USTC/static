from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from .observed_schema import JsonSchema, JsonValue, ObservedNode
from .upstream_contract_comparison import (
    MINIMUM_BREADTH_EVIDENCE,
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

    @property
    def endpoint_names(self) -> set[str]:
        return set(self._roots)

    def observe(self, endpoint: str, payload: JsonValue) -> None:
        self._roots.setdefault(endpoint, ObservedNode()).observe(payload)

    def observe_and_assert_compatible(
        self,
        endpoint: str,
        payload: JsonValue,
        model: UpstreamResponseModel,
    ) -> None:
        self.observe(endpoint, payload)
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
            issues.extend(
                compare_observed_to_expected(
                    endpoint,
                    root,
                    expected_schema(model),
                    check_expected_breadth=True,
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


def publish_contract_artifacts(
    collector: ObservedContractCollector,
    output_dir: Path,
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

    output_dir.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}-", dir=output_dir.parent)
    )
    backup_dir = output_dir.with_name(f".{output_dir.name}.previous")
    try:
        for name, model in sorted(expected_models.items()):
            _write_json(
                temporary_dir / f"{name}.expected.schema.json",
                expected_schema(model),
            )
        for name in sorted(observed_models):
            _write_json(
                temporary_dir / f"{name}.observed.schema.json",
                collector.observed_schema(name),
            )
        _write_json(
            temporary_dir / "contract-report.json",
            {
                "issues": [issue.as_dict() for issue in issues],
                "policy": {
                    "alwaysNull": "warning",
                    "emptyArrayItems": "warning",
                    "expectedNullableButNoObservedNull": (
                        "warning; a single build is insufficient evidence to remove "
                        "long-term nullable semantics"
                    ),
                    "expectedOptionalButAlwaysPresent": {
                        "errorAtObjectInstanceCount": MINIMUM_BREADTH_EVIDENCE,
                        "otherwise": "warning",
                    },
                    "fieldNeverObserved": "warning",
                    "incompatibleObservedShape": "error",
                    "typeBranchNotExercised": "warning",
                    "unobservedEndpoint": "error",
                },
                "summary": {
                    "errors": 0,
                    "warnings": sum(issue.severity == "warning" for issue in issues),
                },
            },
        )

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

    return issues
