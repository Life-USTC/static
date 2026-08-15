from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from pydantic import RootModel, ValidationError

from src.models.api.base import UpstreamBaseModel
from src.models.api.catalog_api_teach_semester_list import TeachSemesterListResponse
from src.observed_contracts import (
    ObservedContractCollector,
    UpstreamContractError,
    log_contract_diagnostics,
    publish_contract_artifacts,
)


class Item(UpstreamBaseModel):
    name: str
    tags: list[int]


class ItemList(RootModel[list[Item]]):
    pass


class OptionalItem(UpstreamBaseModel):
    name: str = "unknown"


class OptionalItemList(RootModel[list[OptionalItem]]):
    pass


class NullableItem(UpstreamBaseModel):
    name: str | None


class IntegerItem(UpstreamBaseModel):
    value: int


class SometimesMissingItem(UpstreamBaseModel):
    name: str = "unknown"


class TreeItem(UpstreamBaseModel):
    name: str
    children: list[TreeItem] | None = None


class ObservedContractTest(unittest.TestCase):
    def test_profiles_nested_lists_empty_arrays_and_null(self) -> None:
        collector = ObservedContractCollector()
        collector.observe(
            "items",
            [
                {"name": "one", "tags": [1, 2]},
                {"name": "two", "tags": []},
            ],
        )

        schema = collector.observed_schema("items")
        item_schema = schema["items"]

        self.assertEqual(item_schema["type"], "object")
        self.assertEqual(item_schema["required"], ["name", "tags"])
        self.assertEqual(
            item_schema["properties"]["tags"]["x-observed-array"],
            {"elements": 2, "emptyInstances": 1, "instances": 2},
        )
        self.assertFalse(collector.verify({"items": ItemList}))

        nullable = ObservedContractCollector()
        nullable.observe("items", {"name": None})
        nullable.observe("items", {"name": "known"})
        self.assertFalse(nullable.verify({"items": NullableItem}))

    def test_rejects_extra_missing_and_mixed_type_fields(self) -> None:
        cases = [
            ([{"name": "one", "tags": [], "extra": True}], "UNDECLARED_FIELD"),
            ([{"name": "one"}], "REQUIRED_FIELD_MISSING"),
            (
                [
                    {"name": "one", "tags": []},
                    {"name": 2, "tags": []},
                ],
                "INCOMPATIBLE_TYPE",
            ),
        ]
        for payload, code in cases:
            with self.subTest(code=code):
                collector = ObservedContractCollector()
                collector.observe("items", payload)
                issues = collector.verify({"items": ItemList})
                self.assertIn(code, {issue.code for issue in issues})

        for incompatible_value in (1.5, True, "1"):
            with self.subTest(incompatible_value=incompatible_value):
                collector = ObservedContractCollector()
                collector.observe("item", {"value": incompatible_value})
                issues = collector.verify({"item": IntegerItem})
                self.assertIn("INCOMPATIBLE_TYPE", {issue.code for issue in issues})

        with self.assertRaises(ValidationError):
            IntegerItem.model_validate({"value": "1"})
        with self.assertRaises(ValidationError):
            TeachSemesterListResponse.model_validate(
                [
                    {
                        "id": "1",
                        "nameZh": "semester",
                        "code": "20261",
                        "start": "2026-08-30",
                        "end": "2027-01-15",
                        "isLast": False,
                    }
                ]
            )

    def test_presence_optionality_and_nullability_are_distinct_failures(self) -> None:
        optional = ObservedContractCollector()
        optional.observe("item", {"name": "one"})
        optional.observe("item", {"name": "two"})
        optional_codes = {
            issue.code
            for issue in optional.verify({"item": OptionalItem})
            if issue.severity == "error"
        }
        self.assertEqual(optional_codes, {"UNNECESSARY_OPTIONAL"})

        nullable = ObservedContractCollector()
        nullable.observe("item", {"name": "one"})
        nullable.observe("item", {"name": "two"})
        nullable_issues = [
            issue
            for issue in nullable.verify({"item": NullableItem})
            if issue.code == "NULLABILITY_UNPROVEN"
        ]
        self.assertEqual(len(nullable_issues), 1)
        self.assertEqual(nullable_issues[0].severity, "warning")
        self.assertIn("2 value(s)", nullable_issues[0].message)
        nullable_errors = {
            issue.code
            for issue in nullable.verify({"item": NullableItem})
            if issue.severity == "error"
        }
        self.assertFalse(nullable_errors)

        sometimes_missing = ObservedContractCollector()
        sometimes_missing.observe("item", {"name": "one"})
        sometimes_missing.observe("item", {})
        self.assertFalse(
            [
                issue
                for issue in sometimes_missing.verify({"item": SometimesMissingItem})
                if issue.severity == "error"
            ]
        )

    def test_optional_hard_failure_requires_independent_fetch_evidence(self) -> None:
        incremental = ObservedContractCollector()
        incremental.require_contexts("items", {"semester=1", "semester=2"})
        incremental.observe(
            "items",
            [{"name": f"item-{index}"} for index in range(100)],
            fetch_context="semester=1",
        )

        issues = incremental.verify({"items": OptionalItemList})
        optional_issue = next(
            issue for issue in issues if issue.code == "UNNECESSARY_OPTIONAL"
        )
        self.assertEqual(optional_issue.severity, "warning")
        self.assertEqual(
            incremental.coverage("items")["missingContexts"], ["semester=2"]
        )

        complete = ObservedContractCollector()
        complete.require_contexts("items", {"semester=1"})
        complete.observe(
            "items",
            [{"name": "one"}, {"name": "two"}],
            fetch_context="semester=1",
        )
        complete_issue = next(
            issue
            for issue in complete.verify({"items": OptionalItemList})
            if issue.code == "UNNECESSARY_OPTIONAL"
        )
        self.assertEqual(complete_issue.severity, "error")

    def test_recursive_evidence_is_combined_for_optionality(self) -> None:
        collector = ObservedContractCollector()
        collector.observe(
            "tree",
            {
                "name": "root",
                "children": [
                    {"name": "leaf-one"},
                    {"name": "leaf-two"},
                ],
            },
        )

        issues = collector.verify({"tree": TreeItem})

        self.assertNotIn("UNNECESSARY_OPTIONAL", {issue.code for issue in issues})

    def test_empty_array_and_always_null_are_explicitly_unverifiable(self) -> None:
        empty = ObservedContractCollector()
        empty.observe("items", [])
        empty_codes = {issue.code for issue in empty.verify({"items": ItemList})}
        self.assertIn("EMPTY_ARRAY_ITEMS_UNPROVEN", empty_codes)

        always_null = ObservedContractCollector()
        always_null.observe("item", {"name": None})
        always_null.observe("item", {"name": None})
        issues = always_null.verify({"item": NullableItem})
        self.assertIn("ALWAYS_NULL", {issue.code for issue in issues})
        self.assertFalse([issue for issue in issues if issue.severity == "error"])

    def test_observed_schema_is_deterministic_across_observation_order(self) -> None:
        payloads = [
            {"name": "one", "tags": [1]},
            {"tags": [2], "name": "two"},
        ]
        first = ObservedContractCollector()
        second = ObservedContractCollector()
        for payload in payloads:
            first.observe("items", payload)
        for payload in reversed(payloads):
            second.observe("items", payload)

        self.assertEqual(
            first.observed_schema("items"), second.observed_schema("items")
        )

    def test_failed_verification_does_not_replace_published_artifacts(self) -> None:
        collector = ObservedContractCollector()
        collector.observe("item", {"name": "one"})
        collector.observe("item", {"name": "two"})

        with tempfile.TemporaryDirectory() as temporary_dir:
            expected_dir = Path(temporary_dir) / "expected"
            diagnostic_dir = Path(temporary_dir) / "diagnostic"
            expected_dir.mkdir()
            diagnostic_dir.mkdir()
            expected_marker = expected_dir / "previous.txt"
            diagnostic_marker = diagnostic_dir / "previous.txt"
            expected_marker.write_text("stable", encoding="utf-8")
            diagnostic_marker.write_text("stable", encoding="utf-8")

            with self.assertRaises(UpstreamContractError):
                publish_contract_artifacts(
                    collector,
                    expected_dir,
                    diagnostic_dir,
                    observed_models={"item": OptionalItem},
                    expected_models={"item": OptionalItem},
                )

            self.assertEqual(expected_marker.read_text(encoding="utf-8"), "stable")
            self.assertEqual(diagnostic_marker.read_text(encoding="utf-8"), "stable")
            self.assertEqual(list(expected_dir.iterdir()), [expected_marker])
            self.assertEqual(list(diagnostic_dir.iterdir()), [diagnostic_marker])

    def test_unobserved_required_endpoint_fails(self) -> None:
        collector = ObservedContractCollector()

        issues = collector.verify({"items": ItemList})

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].code, "ENDPOINT_NOT_OBSERVED")
        self.assertEqual(issues[0].severity, "error")

    def test_successful_artifact_publication_is_deterministic(self) -> None:
        collector = ObservedContractCollector()
        collector.observe("items", [{"name": "one", "tags": [1]}])

        with tempfile.TemporaryDirectory() as temporary_dir:
            expected_dir = Path(temporary_dir) / "expected"
            diagnostic_dir = Path(temporary_dir) / "diagnostic"
            publish_contract_artifacts(
                collector,
                expected_dir,
                diagnostic_dir,
                observed_models={"items": ItemList},
                expected_models={"items": ItemList},
            )
            first_expected = {
                path.name: path.read_bytes() for path in expected_dir.iterdir()
            }
            first_diagnostic = {
                path.name: path.read_bytes() for path in diagnostic_dir.iterdir()
            }
            publish_contract_artifacts(
                collector,
                expected_dir,
                diagnostic_dir,
                observed_models={"items": ItemList},
                expected_models={"items": ItemList},
            )
            second_expected = {
                path.name: path.read_bytes() for path in expected_dir.iterdir()
            }
            second_diagnostic = {
                path.name: path.read_bytes() for path in diagnostic_dir.iterdir()
            }

            self.assertEqual(first_expected, second_expected)
            self.assertEqual(first_diagnostic, second_diagnostic)
            self.assertEqual(set(first_expected), {"items.expected.schema.json"})
            self.assertEqual(
                set(first_diagnostic),
                {"items.observed.schema.json", "contract-report.json"},
            )
            report = json.loads(first_diagnostic["contract-report.json"])
            self.assertEqual(report["coverage"]["items"]["independentFetchCount"], 1)

    def test_warning_summary_is_logged_with_bounded_details(self) -> None:
        collector = ObservedContractCollector()
        collector.observe("item", {"name": "one"})
        issues = collector.verify({"item": NullableItem})

        with self.assertLogs("contract-test", level="WARNING") as logs:
            log_contract_diagnostics(
                issues,
                Path("diagnostics/contract-report.json"),
                logger=logging.getLogger("contract-test"),
                maximum_issue_lines=1,
            )

        output = "\n".join(logs.output)
        self.assertIn("NULLABILITY_UNPROVEN", output)
        self.assertIn("diagnostics/contract-report.json", output)


if __name__ == "__main__":
    unittest.main()
