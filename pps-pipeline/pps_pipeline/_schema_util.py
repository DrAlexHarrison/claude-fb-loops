"""pps_pipeline._schema_util — load + validate against the JSON Schemas.

The three ``schema/*.schema.json`` files are the *formal contract* for the
artifacts (SessionBundle manifest, InterleavedPackage, Assessment). jsonschema
is the single validation mechanism; we keep dataclasses for ergonomic in-code
construction and validate the emitted JSON against the schema at the boundary.
"""

from __future__ import annotations

import functools
import json
import os
from typing import Any

import jsonschema

_SCHEMA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema")


@functools.lru_cache(maxsize=None)
def load_schema(name: str) -> dict:
    """Load a schema file by basename (e.g. ``package.schema.json``)."""
    with open(os.path.join(_SCHEMA_DIR, name), "r", encoding="utf-8") as fh:
        return json.load(fh)


def validation_errors(schema_name: str, instance: Any) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid)."""
    schema = load_schema(schema_name)
    validator = jsonschema.Draft7Validator(schema)
    errs = sorted(validator.iter_errors(instance), key=lambda e: list(e.path))
    out = []
    for e in errs:
        loc = "/".join(str(p) for p in e.path) or "<root>"
        out.append(f"{loc}: {e.message}")
    return out


def validate(schema_name: str, instance: Any) -> None:
    """Raise ``jsonschema.ValidationError`` on the first violation."""
    jsonschema.validate(instance=instance, schema=load_schema(schema_name),
                        cls=jsonschema.Draft7Validator)


def is_valid(schema_name: str, instance: Any) -> bool:
    return not validation_errors(schema_name, instance)
