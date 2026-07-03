from __future__ import annotations

import json
from pathlib import Path

from src.upstream_contracts import UPSTREAM_RESPONSE_MODELS


def export_upstream_schemas(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    exported: list[Path] = []

    for name, model in UPSTREAM_RESPONSE_MODELS.items():
        schema = model.model_json_schema(by_alias=True)
        path = output_dir / f"{name}.schema.json"
        path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        exported.append(path)

    return exported
