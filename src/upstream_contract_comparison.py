from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from .observed_schema import JSON_KIND_ORDER, JsonSchema, ObservedNode
from .upstream_contracts import UpstreamResponseModel

MINIMUM_BREADTH_EVIDENCE = 2


@dataclass(frozen=True)
class ContractIssue:
    severity: str
    code: str
    endpoint: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "endpoint": self.endpoint,
            "message": self.message,
            "path": self.path,
            "severity": self.severity,
        }


class UpstreamContractError(ValueError):
    def __init__(self, issues: list[ContractIssue]):
        self.issues = issues
        lines = ["Upstream contract verification failed:"]
        lines.extend(
            f"- {issue.endpoint} {issue.path}: [{issue.code}] {issue.message}"
            for issue in issues
            if issue.severity == "error"
        )
        super().__init__("\n".join(lines))


def expected_schema(model: UpstreamResponseModel) -> JsonSchema:
    return model.model_json_schema(by_alias=True, mode="validation")


def _resolve(schema: JsonSchema, root: JsonSchema) -> JsonSchema:
    reference = schema.get("$ref")
    if not reference:
        return schema
    if not reference.startswith("#/"):
        raise ValueError(f"Unsupported external JSON Schema reference: {reference}")
    resolved: Any = root
    for part in reference[2:].split("/"):
        resolved = resolved[part.replace("~1", "/").replace("~0", "~")]
    return resolved


def _alternatives(schema: JsonSchema, root: JsonSchema) -> list[JsonSchema]:
    schema = _resolve(schema, root)
    if "anyOf" in schema:
        return [
            nested
            for alternative in schema["anyOf"]
            for nested in _alternatives(alternative, root)
        ]
    return [schema]


def _declared_kinds(schema: JsonSchema, root: JsonSchema) -> set[str]:
    kinds: set[str] = set()
    for alternative in _alternatives(schema, root):
        declared_type = alternative.get("type")
        if isinstance(declared_type, str):
            kinds.add(declared_type)
        elif isinstance(declared_type, list):
            kinds.update(declared_type)
        elif "properties" in alternative:
            kinds.add("object")
        elif "items" in alternative:
            kinds.add("array")
    return kinds


def _kind_is_accepted(kind: str, expected_kinds: set[str]) -> bool:
    return kind in expected_kinds or (kind == "integer" and "number" in expected_kinds)


def _schema_for_kind(
    schema: JsonSchema, root: JsonSchema, kind: str
) -> JsonSchema | None:
    for alternative in _alternatives(schema, root):
        if _kind_is_accepted(kind, _declared_kinds(alternative, root)):
            return _resolve(alternative, root)
    return None


def compare_observed_to_expected(
    endpoint: str,
    observed: ObservedNode,
    expected: JsonSchema,
    *,
    check_expected_breadth: bool,
    optional_mismatch_is_error: bool = False,
) -> list[ContractIssue]:
    issues: list[ContractIssue] = []
    object_instances: Counter[int] = Counter()
    object_presence: dict[int, Counter[str]] = {}
    object_first_path: dict[int, str] = {}

    def collect_object_evidence(
        node: ObservedNode, schema: JsonSchema, path: str
    ) -> None:
        if node.object_count:
            object_schema = _schema_for_kind(schema, expected, "object")
            if object_schema is not None:
                key = id(object_schema)
                object_instances[key] += node.object_count
                object_presence.setdefault(key, Counter()).update(
                    node.property_presence
                )
                object_first_path.setdefault(key, path)
                properties = object_schema.get("properties", {})
                for name, child in node.properties.items():
                    if name in properties:
                        collect_object_evidence(
                            child, properties[name], f"{path}.{name}"
                        )
        if node.array_count and node.items is not None:
            array_schema = _schema_for_kind(schema, expected, "array")
            if array_schema is not None:
                collect_object_evidence(
                    node.items, array_schema.get("items", {}), f"{path}[]"
                )

    collect_object_evidence(observed, expected, "$")

    def add(severity: str, code: str, path: str, message: str) -> None:
        issues.append(ContractIssue(severity, code, endpoint, path, message))

    def visit(node: ObservedNode, schema: JsonSchema, path: str) -> None:
        expected_kinds = _declared_kinds(schema, expected)
        for kind in JSON_KIND_ORDER:
            count = node.kind_counts[kind]
            if count and not _kind_is_accepted(kind, expected_kinds):
                add(
                    "error",
                    "INCOMPATIBLE_TYPE",
                    path,
                    f"observed JSON type {kind!r} {count} time(s), expected "
                    f"{sorted(expected_kinds)!r}",
                )

        if check_expected_breadth:
            if "null" in expected_kinds and not node.kind_counts["null"]:
                add(
                    "warning",
                    "NULLABILITY_UNPROVEN",
                    path,
                    "Pydantic accepts null but this single complete build never used "
                    f"it across {node.value_count} value(s)",
                )
            if node.kind_counts["null"] == node.value_count:
                add(
                    "warning",
                    "ALWAYS_NULL",
                    path,
                    "all observed values are null; the concrete value type is unproven",
                )
            elif node.value_count >= MINIMUM_BREADTH_EVIDENCE:
                for kind in sorted(expected_kinds - {"null"}):
                    observed_count = node.kind_counts[kind]
                    if kind == "number":
                        observed_count += node.kind_counts["integer"]
                    if not observed_count:
                        add(
                            "warning",
                            "TYPE_BRANCH_UNOBSERVED",
                            path,
                            f"Pydantic accepts JSON type {kind!r}, but this build "
                            "did not exercise that union branch",
                        )

        if node.object_count:
            object_schema = _schema_for_kind(schema, expected, "object")
            if object_schema is not None:
                visit_object(node, object_schema, path)
        if node.array_count:
            array_schema = _schema_for_kind(schema, expected, "array")
            if array_schema is not None:
                visit_array(node, array_schema, path)

    def visit_object(node: ObservedNode, schema: JsonSchema, path: str) -> None:
        properties: dict[str, JsonSchema] = schema.get("properties", {})
        required = set(schema.get("required", []))
        additional = schema.get("additionalProperties", True)
        schema_key = id(schema)

        for name, child in sorted(node.properties.items()):
            child_path = f"{path}.{name}"
            child_schema = properties.get(name)
            if child_schema is None:
                if additional is False:
                    add(
                        "error",
                        "UNDECLARED_FIELD",
                        child_path,
                        "raw JSON contains a field not declared by Pydantic",
                    )
                continue
            visit(child, child_schema, child_path)

        for name in sorted(properties):
            child_path = f"{path}.{name}"
            presence = node.property_presence[name]
            if name in required and presence < node.object_count:
                add(
                    "error",
                    "REQUIRED_FIELD_MISSING",
                    child_path,
                    f"required in Pydantic but missing from "
                    f"{node.object_count - presence}/{node.object_count} "
                    "observed object(s)",
                )
            if not check_expected_breadth:
                continue
            if path != object_first_path[schema_key]:
                continue
            total_instances = object_instances[schema_key]
            total_presence = object_presence[schema_key][name]
            if not total_presence:
                add(
                    "warning",
                    "FIELD_NEVER_OBSERVED",
                    child_path,
                    "declared by Pydantic but absent from every observed object",
                )
            elif name not in required and total_presence == total_instances:
                severity = (
                    "error"
                    if optional_mismatch_is_error
                    and total_instances >= MINIMUM_BREADTH_EVIDENCE
                    else "warning"
                )
                add(
                    severity,
                    "UNNECESSARY_OPTIONAL",
                    child_path,
                    "Pydantic allows the field to be missing, but it was present in "
                    f"all {total_instances} observed instance(s) of this object "
                    "contract",
                )

    def visit_array(node: ObservedNode, schema: JsonSchema, path: str) -> None:
        item_schema = schema.get("items", {})
        if node.items is not None:
            visit(node.items, item_schema, f"{path}[]")
        elif check_expected_breadth:
            add(
                "warning",
                "EMPTY_ARRAY_ITEMS_UNPROVEN",
                f"{path}[]",
                "all observed arrays were empty; their item contract was not exercised",
            )

    visit(observed, expected, "$")
    return issues
