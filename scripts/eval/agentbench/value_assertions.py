"""Structured assertions bound to the record's own observed values."""
from __future__ import annotations

import re

OPERATORS = {"equals", "gt", "gte", "lt", "lte", "in", "nonempty", "exists"}
PROJECTION = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+)*$")
MISSING = object()

# capability + field + case -> (operator, expected).  Values are never copied
# from the record's own actual; dynamic ids use predicates instead of literals.
FIELD_RULES = {
    "abstained": ("equals", False),
    "action": ("in", ["保存", "更新", "删除", "cancel"]),
    "agent": ("in", ["search_agent", "trade_agent"]),
    "buyer_id": ("nonempty", True),
    "claim_guard": ("nonempty", True),
    "confirmation.action": ("equals", "cancel"),
    "confirmation.confirmation_id": ("nonempty", True),
    "confirmation.payload": ("exists", True),
    "confirmation.result.order_id": ("nonempty", True),
    "confirmation.result.status": ("in", ["CONFIRMED", "CANCELLED"]),
    "confirmation.snapshot_hash": ("nonempty", True),
    "confirmation.status": ("in", ["pending", "approved", "rejected"]),
    "confirmation_required": ("equals", True),
    "constraint_continuity.inherited": ("nonempty", True),
    "correlation.session_id": ("nonempty", True),
    "deleted": ("equals", "喜欢超轻背包"),
    "dispatch_id": ("nonempty", True),
    "filtered_out": ("exists", True),
    "hit_count": ("gt", 0),
    "hits": ("nonempty", True),
    "hits[].landed_price.currency": ("nonempty", True),
    "hits[].landed_price.freight_major": ("gt", 0),
    "hits[].landed_price.landed_total_major": ("gt", 0),
    "hits[].landed_price.subtotal_major": ("gt", 0),
    "hits[].landed_price.tariff_major": ("gte", 0),
    "insights[].metadata.source_reference": ("nonempty", True),
    "insights[].metadata.source_type": ("nonempty", True),
    "insights[].source": ("nonempty", True),
    "inventory_after": ("exists", True),
    "inventory_before": ("exists", True),
    "message": ("nonempty", True),
    "order": ("nonempty", True),
    "order_count_after": ("exists", True),
    "order_count_before": ("exists", True),
    "policy_fact_statuses": ("exists", True),
    "query_conditions": ("nonempty", True),
    "query_conditions.price_max_major": ("exists", True),
    "query_conditions.ship_to": ("exists", True),
    "query_conditions.sku_spec": ("exists", True),
    "reason": ("nonempty", True),
    "recall_strategy": ("nonempty", True),
    "replacement_kind": ("nonempty", True),
    "result_ref": ("nonempty", True),
    "saved": ("equals", "喜欢轻便背包"),
    "shopping_session_id": ("nonempty", True),
    "statement": ("nonempty", True),
    "status": ("in", ["completed", "pending", "approved", "rejected"]),
    "tool": ("nonempty", True),
    "total_candidates": ("gt", 0),
    "unanswerable": ("equals", True),
    "unverified_product_ids": ("equals", []),
    "updated": ("equals", True),
    "verified_product_ids": ("nonempty", True),
    "violations": ("nonempty", True),
}

ORACLES = {
    ("search.exact_id", "recall_strategy", None): ("equals", "exact_id_lookup"),
    ("search.category_insight", "hit_count", "abstain"): ("equals", 0),
    ("search.category_insight", "abstained", "abstain"): ("equals", True),
    ("search.category_insight", "abstained", None): ("equals", False),
    ("memory.remember", "statement", None): ("equals", "喜欢轻便背包"),
    ("memory.forget", "statement", None): ("equals", "喜欢超轻背包"),
    ("memory.remember", "action", None): ("equals", "保存"),
    ("memory.update", "action", None): ("equals", "更新"),
    ("memory.forget", "action", None): ("equals", "删除"),
    ("memory.forget", "remaining", None): ("equals", 0),
    ("trade.prepare", "confirmation.status", None): ("equals", "pending"),
    ("trade.approve", "confirmation.status", "approved"): ("equals", "approved"),
    ("trade.approve", "confirmation.status", "rejected"): ("equals", "rejected"),
    ("trade.cancel", "confirmation.status", None): ("equals", "approved"),
    ("trade.idempotency", "confirmation.status", None): ("equals", "approved"),
    ("dispatch.search", "agent", None): ("equals", "search_agent"),
    ("dispatch.trade", "agent", None): ("equals", "trade_agent"),
    ("dispatch.search", "status", None): ("equals", "completed"),
    ("dispatch.trade", "status", None): ("equals", "completed"),
    ("trade.prepare", "order_count_before", None): ("equals", 0),
    ("trade.prepare", "order_count_after", None): ("equals", 0),
    ("trade.approve", "order_count_before", "approved"): ("equals", 0),
    ("trade.approve", "order_count_after", "approved"): ("equals", 1),
    ("trade.approve", "order_count_before", "rejected"): ("equals", 0),
    ("trade.approve", "order_count_after", "rejected"): ("equals", 0),
    ("trade.cancel", "order_count_before", None): ("equals", 2),
    ("trade.cancel", "order_count_after", None): ("equals", 2),
    ("trade.idempotency", "order_count_before", None): ("equals", 0),
    ("trade.idempotency", "order_count_after", None): ("equals", 1),
}

def _expected(capability_id: str, field: str, case: str | None):
    return (ORACLES.get((capability_id, field, case))
            or ORACLES.get((capability_id, field, None))
            or FIELD_RULES.get(field))


def build(capability_id: str, field: str, values: dict, case: str | None = None) -> dict:
    actual = values.get(field, MISSING)
    contract = _expected(capability_id, field, case)
    if contract is None:
        raise ValueError(f"{capability_id}:{field}: no contract rule")
    operator, expected = contract
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
        if contract is None:
            errors.append(f"{field}: no contract rule for this capability")
        elif (item.get("operator"), expected) != contract:
            errors.append(f"{field}: operator/expected do not match the capability contract")
        operator = item.get("operator")
        if operator not in OPERATORS:
            errors.append(f"{field}: invalid operator {operator!r}")
        elif operator == "equals" and actual != expected:
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
