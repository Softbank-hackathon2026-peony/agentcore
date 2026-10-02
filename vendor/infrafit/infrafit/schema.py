"""schemas/infrafit.schema.json으로 단계 출력을 검증한다."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import jsonschema

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "infrafit.schema.json"


class SchemaError(ValueError):
    """단계 출력이 스키마를 통과하지 못했다."""


@lru_cache(maxsize=1)
def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate(def_name: str, data: dict) -> None:
    schema = _schema()
    if def_name not in schema["$defs"]:
        raise SchemaError(f"{def_name}: $defs에 없는 정의")
    sub = {"$schema": schema["$schema"], "$defs": schema["$defs"], "$ref": f"#/$defs/{def_name}"}
    validator = jsonschema.Draft202012Validator(sub, format_checker=jsonschema.FormatChecker())
    errors = sorted(validator.iter_errors(data), key=lambda e: [str(p) for p in e.path])
    if errors:
        parts = [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in errors[:10]]
        raise SchemaError(f"{def_name}: " + "; ".join(parts))
