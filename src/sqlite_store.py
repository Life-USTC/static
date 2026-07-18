from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from types import NoneType, UnionType
from typing import Union, get_args, get_origin

from pydantic import BaseModel, RootModel

SCHEMA_VERSION = 5
SNAPSHOT_FILENAME = "life-ustc-static.sqlite"
GUESSES_FILENAME = "life-ustc-static-guesses.sqlite"

Scalar = str | int | float | bool
JsonScalar = str | int | float | bool | None
JsonValue = Mapping[str, object] | list[object] | JsonScalar


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _column_type(annotation: object) -> str:
    annotation = _unwrap_optional(annotation)
    if annotation is bool:
        return "INTEGER"
    if annotation is int:
        return "INTEGER"
    if annotation is float:
        return "REAL"
    if annotation is str:
        return "TEXT"
    return "TEXT"


def _is_scalar_annotation(annotation: object) -> bool:
    annotation = _unwrap_optional(annotation)
    return annotation in {str, int, float, bool}


def _unwrap_optional(annotation: object) -> object:
    origin = get_origin(annotation)
    if origin in {UnionType, Union}:
        args = [arg for arg in get_args(annotation) if arg is not NoneType]
        if len(args) == 1:
            return args[0]
    return annotation


def _list_item_annotation(annotation: object) -> object | None:
    annotation = _unwrap_optional(annotation)
    if get_origin(annotation) is list:
        args = get_args(annotation)
        return args[0] if args else str
    return None


def _is_model_annotation(annotation: object) -> bool:
    annotation = _unwrap_optional(annotation)
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _field_external_name(field_name: str, field: object) -> str:
    alias = getattr(field, "alias", None)
    return alias or field_name


def _scalar_value(value: object) -> object:
    if isinstance(value, bool):
        return int(value)
    return value


def _json_column_type(value: object) -> str:
    if value is None:
        return "TEXT"
    if isinstance(value, bool):
        return "INTEGER"
    if isinstance(value, int):
        return "INTEGER"
    if isinstance(value, float):
        return "REAL"
    return "TEXT"


class SQLiteModelStore:
    def __init__(self, path: Path | str, *, reset: bool = True):
        self.path = path
        if path != ":memory:":
            self.path = Path(path)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if reset and self.path.exists():
                self.path.unlink()

        self.conn = sqlite3.connect(self.path)
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = DELETE")
        self.conn.execute("PRAGMA synchronous = FULL")
        self._known_columns: dict[str, set[str]] = {}
        self._create_base_schema()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def _create_base_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS upstream_fetches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                method TEXT NOT NULL,
                url TEXT NOT NULL,
                context TEXT,
                ok INTEGER NOT NULL,
                error TEXT,
                fetched_at TEXT NOT NULL
            );
            """
        )
        self.conn.executemany(
            """
            INSERT INTO metadata(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            [
                ("schema_version", str(SCHEMA_VERSION)),
                ("generated_at", datetime.now(UTC).isoformat()),
            ],
        )

    def delete_fetches(self, fetch_ids: list[int]) -> None:
        if not fetch_ids:
            return

        placeholders = ", ".join("?" for _ in fetch_ids)
        for table_name in self._tables_with_column("fetch_id"):
            self.conn.execute(
                f"DELETE FROM {_quote_identifier(table_name)} "
                f"WHERE fetch_id IN ({placeholders})",
                fetch_ids,
            )
        self.conn.execute(
            f"DELETE FROM upstream_fetches WHERE id IN ({placeholders})",
            fetch_ids,
        )

    def _tables_with_column(self, column_name: str) -> list[str]:
        table_names = [
            row[0]
            for row in self.conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
        ]
        result = []
        for table_name in table_names:
            columns = {
                row[1]
                for row in self.conn.execute(
                    f"PRAGMA table_info({_quote_identifier(table_name)})"
                )
            }
            if column_name in columns:
                result.append(table_name)
        return result

    def record_fetch(
        self,
        *,
        source: str,
        method: str,
        url: str,
        context: Mapping[str, Scalar | None] | None = None,
        ok: bool = True,
        error: str | None = None,
    ) -> int:
        cursor = self.conn.execute(
            """
            INSERT INTO upstream_fetches(
                source,
                method,
                url,
                context,
                ok,
                error,
                fetched_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                method,
                url,
                "&".join(
                    f"{key}={value}" for key, value in sorted((context or {}).items())
                )
                if context
                else None,
                int(ok),
                error,
                datetime.now(UTC).isoformat(),
            ),
        )
        return int(cursor.lastrowid)

    def put_metadata(self, items: Mapping[str, str | int]) -> None:
        self.conn.executemany(
            """
            INSERT INTO metadata(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            [(key, str(value)) for key, value in items.items()],
        )

    def register_response_model(
        self,
        *,
        table_name: str,
        response_model: type[BaseModel],
    ) -> None:
        self._ensure_response_schema(
            table_name,
            response_model,
            base_columns={"fetch_id": "INTEGER NOT NULL"},
            model_stack=(),
        )

    def store_response(
        self,
        *,
        table_name: str,
        response: BaseModel,
        fetch_id: int,
        context: Mapping[str, Scalar | None] | None = None,
    ) -> int:
        context = context or {}

        if isinstance(response, RootModel):
            root = response.root
            if isinstance(root, list):
                count = 0
                for position, item in enumerate(root):
                    if isinstance(item, BaseModel):
                        self._insert_model(
                            table_name,
                            item,
                            fetch_id=fetch_id,
                            context=context,
                            position=position,
                        )
                        count += 1
                return count
            if isinstance(root, BaseModel):
                self._insert_model(table_name, root, fetch_id=fetch_id, context=context)
                return 1
            return 0

        self._insert_model(table_name, response, fetch_id=fetch_id, context=context)
        return 1

    def store_json_response(
        self,
        *,
        table_name: str,
        payload: JsonValue,
        fetch_id: int,
        context: Mapping[str, Scalar | None] | None = None,
    ) -> int:
        return self._insert_json_value(
            table_name,
            payload,
            fetch_id=fetch_id,
            context=context or {},
        )

    def _ensure_response_schema(
        self,
        table_name: str,
        model_type: type[BaseModel],
        *,
        base_columns: Mapping[str, str],
        model_stack: tuple[type[BaseModel], ...],
    ) -> None:
        if issubclass(model_type, RootModel):
            root_annotation = model_type.model_fields["root"].annotation
            item_annotation = _list_item_annotation(root_annotation)
            if item_annotation is not None:
                self._ensure_annotation_schema(
                    table_name,
                    item_annotation,
                    base_columns={
                        **base_columns,
                        "position": "INTEGER NOT NULL",
                    },
                    model_stack=model_stack,
                )
                return

            self._ensure_annotation_schema(
                table_name,
                root_annotation,
                base_columns=base_columns,
                model_stack=model_stack,
            )
            return

        self._ensure_model_schema(
            table_name,
            model_type,
            base_columns=base_columns,
            model_stack=model_stack,
        )

    def _ensure_annotation_schema(
        self,
        table_name: str,
        annotation: object,
        *,
        base_columns: Mapping[str, str],
        model_stack: tuple[type[BaseModel], ...],
    ) -> None:
        annotation = _unwrap_optional(annotation)
        if _is_model_annotation(annotation):
            self._ensure_model_schema(
                table_name,
                annotation,
                base_columns=base_columns,
                model_stack=model_stack,
            )
            return

        self._ensure_table(
            table_name,
            {
                **base_columns,
                "value": _column_type(annotation),
            },
        )

    def _ensure_model_schema(
        self,
        table_name: str,
        model_type: type[BaseModel],
        *,
        base_columns: Mapping[str, str],
        model_stack: tuple[type[BaseModel], ...],
    ) -> None:
        scalar_columns = dict(base_columns)
        nested_items: list[tuple[str, object, bool]] = []
        include_nested = model_type not in model_stack

        for field_name, field in model_type.model_fields.items():
            external_name = _field_external_name(field_name, field)
            annotation = field.annotation
            list_item = _list_item_annotation(annotation)

            if list_item is not None:
                if include_nested:
                    nested_items.append((external_name, list_item, True))
                continue

            if _is_model_annotation(annotation):
                if include_nested:
                    nested_items.append((external_name, annotation, False))
                continue

            scalar_columns[external_name] = _column_type(annotation)

        self._ensure_table(table_name, scalar_columns)

        if not include_nested:
            return

        next_stack = (*model_stack, model_type)
        for field_name, annotation, is_list in nested_items:
            child_base_columns = {
                "fetch_id": "INTEGER NOT NULL",
                "parent_store_id": "INTEGER NOT NULL",
            }
            if is_list:
                child_base_columns["position"] = "INTEGER NOT NULL"
            self._ensure_annotation_schema(
                f"{table_name}_{field_name}",
                annotation,
                base_columns=child_base_columns,
                model_stack=next_stack,
            )

    def _ensure_table(self, table_name: str, columns: Mapping[str, str]) -> None:
        if table_name not in self._known_columns:
            self.conn.execute(
                f"CREATE TABLE IF NOT EXISTS {_quote_identifier(table_name)} "
                '("store_id" INTEGER PRIMARY KEY AUTOINCREMENT)'
            )
            existing = {
                row[1]
                for row in self.conn.execute(
                    f"PRAGMA table_info({_quote_identifier(table_name)})"
                )
            }
            self._known_columns[table_name] = existing

        for name, column_type in columns.items():
            if name in self._known_columns[table_name]:
                continue
            self.conn.execute(
                f"ALTER TABLE {_quote_identifier(table_name)} "
                f"ADD COLUMN {_quote_identifier(name)} {column_type}"
            )
            self._known_columns[table_name].add(name)

    def _insert_model(
        self,
        table_name: str,
        model: BaseModel,
        *,
        fetch_id: int,
        context: Mapping[str, Scalar | None],
        parent_store_id: int | None = None,
        position: int | None = None,
    ) -> int:
        scalar_columns: dict[str, str] = {
            "fetch_id": "INTEGER NOT NULL",
        }
        scalar_values: dict[str, object] = {"fetch_id": fetch_id}

        if parent_store_id is not None:
            scalar_columns["parent_store_id"] = "INTEGER NOT NULL"
            scalar_values["parent_store_id"] = parent_store_id

        if position is not None:
            scalar_columns["position"] = "INTEGER NOT NULL"
            scalar_values["position"] = position

        for key, value in context.items():
            scalar_columns[key] = (
                _column_type(type(value)) if value is not None else "TEXT"
            )
            scalar_values[key] = _scalar_value(value)

        nested_items: list[tuple[str, object, object]] = []
        for field_name, field in model.__class__.model_fields.items():
            external_name = _field_external_name(field_name, field)
            annotation = field.annotation
            value = getattr(model, field_name)
            list_item = _list_item_annotation(annotation)

            if list_item is not None:
                nested_items.append((external_name, list_item, value))
                continue

            if _is_model_annotation(annotation):
                nested_items.append((external_name, annotation, value))
                continue

            scalar_columns[external_name] = _column_type(annotation)
            scalar_values[external_name] = _scalar_value(value)

        self._ensure_table(table_name, scalar_columns)
        column_names = list(scalar_values.keys())
        placeholders = ", ".join("?" for _ in column_names)
        cursor = self.conn.execute(
            f"INSERT INTO {_quote_identifier(table_name)} "
            f"({', '.join(_quote_identifier(name) for name in column_names)}) "
            f"VALUES({placeholders})",
            [scalar_values[name] for name in column_names],
        )
        store_id = int(cursor.lastrowid)

        for field_name, annotation, value in nested_items:
            if value is None:
                continue
            child_table_name = f"{table_name}_{field_name}"
            self._insert_nested(
                child_table_name,
                value,
                annotation,
                fetch_id=fetch_id,
                context=context,
                parent_store_id=store_id,
            )

        return store_id

    def _insert_nested(
        self,
        table_name: str,
        value: object,
        annotation: object,
        *,
        fetch_id: int,
        context: Mapping[str, Scalar | None],
        parent_store_id: int,
    ) -> None:
        annotation = _unwrap_optional(annotation)

        if isinstance(value, list):
            for position, item in enumerate(value):
                if isinstance(item, BaseModel):
                    self._insert_model(
                        table_name,
                        item,
                        fetch_id=fetch_id,
                        context=context,
                        parent_store_id=parent_store_id,
                        position=position,
                    )
                else:
                    self._insert_scalar_child(
                        table_name,
                        item,
                        annotation,
                        fetch_id=fetch_id,
                        context=context,
                        parent_store_id=parent_store_id,
                        position=position,
                    )
            return

        if isinstance(value, BaseModel):
            self._insert_model(
                table_name,
                value,
                fetch_id=fetch_id,
                context=context,
                parent_store_id=parent_store_id,
            )

    def _insert_scalar_child(
        self,
        table_name: str,
        value: object,
        annotation: object,
        *,
        fetch_id: int,
        context: Mapping[str, Scalar | None],
        parent_store_id: int,
        position: int,
    ) -> None:
        columns: dict[str, str] = {
            "fetch_id": "INTEGER NOT NULL",
            "parent_store_id": "INTEGER NOT NULL",
            "position": "INTEGER NOT NULL",
            "value": _column_type(annotation),
        }
        values: dict[str, object] = {
            "fetch_id": fetch_id,
            "parent_store_id": parent_store_id,
            "position": position,
            "value": _scalar_value(value),
        }

        for key, context_value in context.items():
            columns[key] = (
                _column_type(type(context_value))
                if context_value is not None
                else "TEXT"
            )
            values[key] = _scalar_value(context_value)

        self._ensure_table(table_name, columns)
        column_names = list(values.keys())
        placeholders = ", ".join("?" for _ in column_names)
        self.conn.execute(
            f"INSERT INTO {_quote_identifier(table_name)} "
            f"({', '.join(_quote_identifier(name) for name in column_names)}) "
            f"VALUES({placeholders})",
            [values[name] for name in column_names],
        )

    def _insert_json_value(
        self,
        table_name: str,
        value: JsonValue,
        *,
        fetch_id: int,
        context: Mapping[str, Scalar | None],
        parent_store_id: int | None = None,
        position: int | None = None,
    ) -> int:
        if isinstance(value, Mapping):
            self._insert_json_object(
                table_name,
                value,
                fetch_id=fetch_id,
                context=context,
                parent_store_id=parent_store_id,
                position=position,
            )
            return 1

        if isinstance(value, list):
            count = 0
            for item_position, item in enumerate(value):
                count += self._insert_json_value(
                    table_name,
                    item,  # type: ignore[arg-type]
                    fetch_id=fetch_id,
                    context=context,
                    parent_store_id=parent_store_id,
                    position=item_position,
                )
            return count

        self._insert_json_scalar(
            table_name,
            value,
            fetch_id=fetch_id,
            context=context,
            parent_store_id=parent_store_id,
            position=position,
        )
        return 1

    def _json_base_row(
        self,
        *,
        fetch_id: int,
        context: Mapping[str, Scalar | None],
        parent_store_id: int | None,
        position: int | None,
    ) -> tuple[dict[str, str], dict[str, object]]:
        columns: dict[str, str] = {"fetch_id": "INTEGER NOT NULL"}
        values: dict[str, object] = {"fetch_id": fetch_id}

        if parent_store_id is not None:
            columns["parent_store_id"] = "INTEGER NOT NULL"
            values["parent_store_id"] = parent_store_id
        if position is not None:
            columns["position"] = "INTEGER NOT NULL"
            values["position"] = position

        for key, context_value in context.items():
            columns[key] = _json_column_type(context_value)
            values[key] = _scalar_value(context_value)

        return columns, values

    def _insert_json_object(
        self,
        table_name: str,
        value: Mapping[str, object],
        *,
        fetch_id: int,
        context: Mapping[str, Scalar | None],
        parent_store_id: int | None,
        position: int | None,
    ) -> int:
        columns, values = self._json_base_row(
            fetch_id=fetch_id,
            context=context,
            parent_store_id=parent_store_id,
            position=position,
        )
        nested_items: list[tuple[str, JsonValue]] = []

        for key, item in value.items():
            if isinstance(item, Mapping | list):
                nested_items.append((key, item))  # type: ignore[arg-type]
                continue
            columns[key] = _json_column_type(item)
            values[key] = _scalar_value(item)

        store_id = self._insert_row(table_name, columns, values)
        for key, item in nested_items:
            self._insert_json_value(
                f"{table_name}_{key}",
                item,
                fetch_id=fetch_id,
                context=context,
                parent_store_id=store_id,
            )
        return store_id

    def _insert_json_scalar(
        self,
        table_name: str,
        value: JsonScalar,
        *,
        fetch_id: int,
        context: Mapping[str, Scalar | None],
        parent_store_id: int | None,
        position: int | None,
    ) -> None:
        columns, values = self._json_base_row(
            fetch_id=fetch_id,
            context=context,
            parent_store_id=parent_store_id,
            position=position,
        )
        columns["value"] = _json_column_type(value)
        values["value"] = _scalar_value(value)
        self._insert_row(table_name, columns, values)

    def _insert_row(
        self, table_name: str, columns: Mapping[str, str], values: Mapping[str, object]
    ) -> int:
        self._ensure_table(table_name, columns)
        column_names = list(values.keys())
        placeholders = ", ".join("?" for _ in column_names)
        cursor = self.conn.execute(
            f"INSERT INTO {_quote_identifier(table_name)} "
            f"({', '.join(_quote_identifier(name) for name in column_names)}) "
            f"VALUES({placeholders})",
            [values[name] for name in column_names],
        )
        return int(cursor.lastrowid)
