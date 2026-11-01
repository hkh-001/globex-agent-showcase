"""Structured assertions bound to the record's own observed values."""
from __future__ import annotations

import re

OPERATORS = {"equals", "gt", "gte", "lt", "lte", "in", "nonempty", "exists"}
PROJECTION = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")
MISSING = object()

# capability + field + case -> (operator, expected).  Values are never copied
# from the record's own actual; dynamic ids use predicates instead of literals.
ORACLES = {
    ("search.catalog", "hit_count", None): ("gt", 0),
    ("search.exact_id", "recall_strategy", None): ("equals", "exact_id_lookup"),
    ("search.category_insight", "hit_count", None): ("gt", 0),
    ("search.category_insight", "abstained", None): ("equals", False),
    ("search.category_insight", "hit_count", "abstain"): ("equals", 0),
    ("search.category_insight", "abstained", "abstain"): ("equals", True),
    ("search.category_insight", "unanswerable", "abstain"): ("equals", True),
    ("memory.remember", "saved", None): ("equals", "喜欢轻便背包"),
    ("memory.remember", "action", None): ("equals", "保存"),
    ("memory.remember", "statement", None): ("equals", "喜欢轻便背包"),
    ("memory.update", "updated", None): ("equals", True),
    ("memory.update", "stored_statement", None): ("equals", "喜欢超轻背包"),
    ("memory.update", "action", None): ("equals", "更新"),
    ("memory.forget", "deleted", None): ("equals", "喜欢超轻背包"),
    ("memory.forget", "remaining", None): ("equals", 0),
    ("memory.forget", "action", None): ("equals", "删除"),
    ("memory.forget", "statement", None): ("equals", "喜欢超轻背包"),
    ("trade.prepare", "confirmation.status", None): ("equals", "pending"),
    ("trade.prepare", "confirmation_required", None): ("equals", True),
    ("trade.approve", "confirmation.status", "approved"): ("equals", "approved"),
    ("trade.approve", "confirmation.status", "rejected"): ("equals", "rejected"),
    ("trade.cancel", "confirmation.status", None): ("equals", "approved"),
    ("trade.cancel", "confirmation.action", None): ("equals", "cancel"),
    ("trade.idempotency", "confirmation.status", None): ("equals", "approved"),
    ("dispatch.search", "agent", None): ("equals", "search_agent"),
    ("dispatch.trade", "agent", None): ("equals", "trade_agent"),
    ("dispatch.search", "status", None): ("equals", "completed"),
    ("dispatch.trade", "status", None): ("equals", "completed"),
}


def _expected(capability_id: str, field: str, case: str | None):
    return (ORACLES.get((capability_id, field, case))
            or ORACLES.get((capability_id, field, None)))


def build(capability_id: str, field: str, values: dict, case: str | None = None) -> dict:
    actual = values.get(field, MISSING)
    contract = _expected(capability_id, field, case)
    if contract is not None:
        operator, expected = contract
    elif isinstance(actual, list):
        operator, expected = ("nonempty", True) if field == "hits" else ("exists", True)
    elif isinstance(actual, dict):
        operator, expected = "nonempty", True
    elif actual is None or actual is MISSING:
        operator, expected = "exists", False
    else:
        operator, expected = "exists", True
    return {"field": field, "operator": operator,
            "actual": None if actual is MISSING else actual, "expected": expected}


def validate(required: list[str], assertions, observed_values: dict,
             capability_id: str, case: str | None = None) -> list[str]:
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
        projection = item.get("projection")
        if projection is not None and not PROJECTION.match(str(projection)):
            errors.append(f"{field}: invalid projection {projection!r}")
        actual, expected = item.get("actual"), item.get("expected")
        if field not in observed_values:
            errors.append(f"{field}: no observed source value")
            continue
        observed = observed_values[field]
        if observed != actual:
            errors.append(f"{field}: recorded actual {actual!r} != observed {observed!r}")
        contract = _expected(capability_id, field, case)
        if contract is not None and (item.get("operator"), expected) != contract:
            errors.append(f"{field}: operator/expected do not match the capability contract")
        operator = item.get("operator")
        if operator not in OPERATORS:
            errors.append(f"{field}: invalid operator {operator!r}")
        elif operator == "equals" and actual != expected:
            errors.append(f"{field}: expected {expected!r} != actual {actual!r}")
        elif operator == "gt" and not (isinstance(actual, (int, float)) and actual > expected):
            errors.append(f"{field}: expected > {expected!r}, got {actual!r}")
        elif operator == "nonempty" and not actual:
            errors.append(f"{field}: value is empty")
        elif operator == "exists" and actual is None:
            errors.append(f"{field}: value is missing")
    for field in required:
        if field not in by_field:
            errors.append(f"required field {field} has no value assertion")
    return errors
