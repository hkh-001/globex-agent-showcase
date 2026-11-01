"""Structured assertions that bind each required observation field to a value."""
from __future__ import annotations

import re

OPERATORS = {"equals", "gt", "gte", "lt", "lte", "in", "nonempty", "exists"}
PROJECTION = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")


def _resolve(payload, path: str):
    current = payload
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def build(field: str, payload, values) -> dict:
    """Build one assertion for a required field, using a projection when needed."""
    projection = None
    if field.startswith("insights[].metadata."):
        name = field.rsplit(".", 1)[-1]
        actual = values.get(name)
        projection = f"first.metadata.{name}"
    elif field == "insights[].source":
        actual = values.get("source")
        projection = "first.source"
    elif field == "hits":
        hits = values.get("hits") or (payload.get("hits") if isinstance(payload, dict) else []) or []
        actual = (hits[0] or {}).get("product_id") if hits else None
        projection = "first.product_id"
    elif field.startswith("hits[].landed_price."):
        name = field.rsplit(".", 1)[-1]
        actual = values.get(f"landed_price.{name}", values.get(name))
        projection = f"first.landed_price.{name}"
    elif field in values:
        actual = values[field]
    else:
        actual = _resolve(payload, field)
        projection = field if actual is not None else None
    if isinstance(actual, list):
        operator, expected = ("nonempty", True) if field == "hits" else ("equals", actual)
    elif isinstance(actual, dict):
        operator, expected = "nonempty", True
    elif actual is None:
        operator, expected = "exists", False
    else:
        operator, expected = "equals", actual
    return {"field": field, "projection": projection, "operator": operator,
            "actual": actual, "expected": expected}


def validate(required: list[str], assertions) -> list[str]:
    errors = []
    if not isinstance(assertions, list):
        return ["value_assertions must be a list"]
    by_field = {}
    for item in assertions:
        field = item.get("field")
        if field not in required:
            errors.append(f"unrelated assertion field {field!r}")
            continue
        if field in by_field:
            errors.append(f"duplicate assertion for {field}")
        by_field[field] = item
        operator = item.get("operator")
        if operator not in OPERATORS:
            errors.append(f"{field}: invalid operator {operator!r}")
            continue
        projection = item.get("projection")
        if projection is not None and not PROJECTION.match(str(projection)):
            errors.append(f"{field}: invalid projection {projection!r}")
        actual, expected = item.get("actual"), item.get("expected")
        if operator == "equals" and actual != expected:
            errors.append(f"{field}: expected {expected!r} != actual {actual!r}")
        elif operator == "gt" and not (isinstance(actual, (int, float)) and actual > expected):
            errors.append(f"{field}: expected > {expected!r}, got {actual!r}")
        elif operator == "gte" and not (isinstance(actual, (int, float)) and actual >= expected):
            errors.append(f"{field}: expected >= {expected!r}, got {actual!r}")
        elif operator == "lt" and not (isinstance(actual, (int, float)) and actual < expected):
            errors.append(f"{field}: expected < {expected!r}, got {actual!r}")
        elif operator == "lte" and not (isinstance(actual, (int, float)) and actual <= expected):
            errors.append(f"{field}: expected <= {expected!r}, got {actual!r}")
        elif operator == "in" and actual not in (expected or []):
            errors.append(f"{field}: {actual!r} not in expected set")
        elif operator == "nonempty" and not actual:
            errors.append(f"{field}: value is empty")
        elif operator == "exists" and actual is None:
            errors.append(f"{field}: value is missing")
    for field in required:
        if field not in by_field:
            errors.append(f"required field {field} has no value assertion")
    return errors
