"""Executable, fail-closed semantics for Stage 2 deterministic checks."""
from __future__ import annotations

from typing import Any, Iterable

OPERATORS = {
    "scalar_equals", "scalar_gt", "exists", "nonempty",
    "contains_any", "contains_all", "all_equal", "any_gt",
}


class Missing:
    pass


MISSING = Missing()


def _project(value: Any, path: str) -> Any:
    values = [value]
    for part in path.split(".") if path else []:
        many = part.endswith("[]")
        key = part[:-2] if many else part
        next_values: list[Any] = []
        for current in values:
            if not isinstance(current, dict) or key not in current:
                return MISSING
            child = current[key]
            if many:
                if not isinstance(child, list):
                    return MISSING
                next_values.extend(child)
            else:
                next_values.append(child)
        values = next_values
    return values if "[]" in path else (values[0] if values else MISSING)


def _event_payloads(events: Iterable[dict[str, Any]], selector: str) -> list[dict[str, Any]]:
    try:
        event_type, tool = selector.split(":", 1)
    except ValueError:
        return []
    return [event["payload"] for event in events
            if event.get("type") == event_type
            and isinstance(event.get("payload"), dict)
            and event["payload"].get("tool") == tool]


def _evaluate(operator: str, actual: Any, expected: Any) -> bool:
    if operator == "exists":
        return actual is not MISSING
    if actual is MISSING:
        return False
    if operator == "scalar_equals":
        return not isinstance(actual, (list, dict)) and actual == expected
    if operator == "scalar_gt":
        return type(actual) in {int, float} and actual > expected
    if operator == "nonempty":
        return bool(actual) is bool(expected)
    if operator == "contains_any":
        return isinstance(actual, list) and isinstance(expected, list) and bool(set(actual) & set(expected))
    if operator == "contains_all":
        return isinstance(actual, list) and isinstance(expected, list) and set(expected) <= set(actual)
    if operator == "all_equal":
        return isinstance(actual, list) and bool(actual) and all(item == expected for item in actual)
    if operator == "any_gt":
        return isinstance(actual, list) and any(type(item) in {int, float} and item > expected for item in actual)
    return False


def validate_check(check: dict[str, Any]) -> str | None:
    required = {"kind", "selector", "field", "operator", "expected"}
    if set(check) != required:
        return "check_schema"
    if check["kind"] not in {"event", "tool"}:
        return "check_kind"
    if check["operator"] not in OPERATORS:
        return "check_operator"
    if not all(isinstance(check[key], str) and check[key] for key in ("selector", "field", "operator")):
        return "check_text"
    return None


def score(checks: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    failures: list[str] = []
    evidence_errors: list[str] = []
    invoked_tools = [event.get("payload", {}).get("tool") for event in events
                     if event.get("type") == "tool.invoke" and isinstance(event.get("payload"), dict)]
    for index, check in enumerate(checks):
        problem = validate_check(check)
        if problem:
            evidence_errors.append(f"check[{index}]:{problem}")
            continue
        if check["kind"] == "tool":
            if check["selector"] != "execution" or check["field"] != "invoked_tools":
                evidence_errors.append(f"check[{index}]:unknown_tool_projection")
                continue
            actual = invoked_tools
        else:
            payloads = _event_payloads(events, check["selector"])
            if not payloads:
                failures.append(f"check[{index}]:selector_not_observed")
                continue
            # Repeated calls are allowed, but the final matching observation is
            # authoritative.  An earlier good result must not hide a later bad
            # result.  Array quantifiers apply inside that final observation.
            actual = _project(payloads[-1], check["field"])
        if not _evaluate(check["operator"], actual, check["expected"]):
            failures.append(f"check[{index}]:failed")
    if evidence_errors:
        return {"status": "evidence_invalid", "passed": False,
                "evidence_errors": evidence_errors, "failures": failures}
    return {"status": "passed" if not failures else "task_failed", "passed": not failures,
            "evidence_errors": [], "failures": failures}
