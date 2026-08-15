from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

type JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None
type JsonSchema = dict[str, Any]

JSON_KIND_ORDER = (
    "null",
    "boolean",
    "integer",
    "number",
    "string",
    "array",
    "object",
)


def _json_kind(value: JsonValue) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


@dataclass
class ObservedNode:
    value_count: int = 0
    kind_counts: Counter[str] = field(default_factory=Counter)
    object_count: int = 0
    property_presence: Counter[str] = field(default_factory=Counter)
    properties: dict[str, ObservedNode] = field(default_factory=dict)
    array_count: int = 0
    array_element_count: int = 0
    empty_array_count: int = 0
    items: ObservedNode | None = None

    def observe(self, value: JsonValue) -> None:
        kind = _json_kind(value)
        self.value_count += 1
        self.kind_counts[kind] += 1

        if isinstance(value, dict):
            self.object_count += 1
            for name in sorted(value):
                self.property_presence[name] += 1
                self.properties.setdefault(name, ObservedNode()).observe(value[name])
        elif isinstance(value, list):
            self.array_count += 1
            self.array_element_count += len(value)
            if not value:
                self.empty_array_count += 1
            for item in value:
                if self.items is None:
                    self.items = ObservedNode()
                self.items.observe(item)

    def to_json_schema(self) -> JsonSchema:
        alternatives = [
            self._schema_for_kind(kind)
            for kind in JSON_KIND_ORDER
            if self.kind_counts[kind]
        ]
        schema = alternatives[0] if len(alternatives) == 1 else {"anyOf": alternatives}
        schema["x-observed"] = {
            "types": {
                kind: self.kind_counts[kind]
                for kind in JSON_KIND_ORDER
                if self.kind_counts[kind]
            },
            "values": self.value_count,
        }
        return schema

    def _schema_for_kind(self, kind: str) -> JsonSchema:
        if kind == "object":
            required = [
                name
                for name in sorted(self.properties)
                if self.property_presence[name] == self.object_count
            ]
            schema: JsonSchema = {
                "additionalProperties": False,
                "properties": {
                    name: self.properties[name].to_json_schema()
                    for name in sorted(self.properties)
                },
                "type": "object",
                "x-observed-object": {
                    "instances": self.object_count,
                    "propertyPresence": {
                        name: self.property_presence[name]
                        for name in sorted(self.property_presence)
                    },
                },
            }
            if required:
                schema["required"] = required
            return schema
        if kind == "array":
            return {
                "items": self.items.to_json_schema() if self.items else {},
                "type": "array",
                "x-observed-array": {
                    "elements": self.array_element_count,
                    "emptyInstances": self.empty_array_count,
                    "instances": self.array_count,
                },
            }
        return {"type": kind}
