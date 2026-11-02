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


def _is_scalar(value: Any) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


def _scalar_list(value: Any) -> bool:
    return isinstance(value, list) and all(_is_scalar(item) for item in value)


def _in_scope(record: dict[str, Any], scope: dict[str, str] | None) -> bool:
    if not scope:
        return True
    if "shopping_session_id" in scope and record.get("shopping_session_id") != scope["shopping_session_id"]:
        return False
    if "run_id" in scope:
        run_id = record.get("run_id") or (record.get("correlation") or {}).get("run_id")
        if run_id != scope["run_id"]:
            return False
    return True


def _evaluate(operator: str, actual: Any, expected: Any) -> bool:
    if operator == "exists":
        return actual is not MISSING
    if actual is MISSING:
        return False
    if operator == "scalar_equals":
        return _is_scalar(actual) and _is_scalar(expected) and actual == expected
    if operator == "scalar_gt":
        return type(actual) in {int, float} and actual > expected
    if operator == "nonempty":
        return isinstance(expected, bool) and bool(actual) is expected
    if operator == "contains_any":
        return (_scalar_list(actual) and _scalar_list(expected)
                and any(item in expected for item in actual))
    if operator == "contains_all":
        return (_scalar_list(actual) and _scalar_list(expected)
                and all(item in actual for item in expected))
    if operator == "all_equal":
        return _scalar_list(actual) and _is_scalar(expected) and bool(actual) and all(
            item == expected for item in actual)
    if operator == "any_gt":
        return isinstance(actual, list) and any(type(item) in {int, float} and item > expected for item in actual)
    return False


def validate_check(check: dict[str, Any]) -> str | None:
    required = {"kind", "selector", "field", "operator", "expected"}
    if set(check) != required:
        return "check_schema"
    if check["kind"] not in {"event", "tool", "tool_result"}:
        return "check_kind"
    if check["operator"] not in OPERATORS:
        return "check_operator"
    if not all(isinstance(check[key], str) and check[key] for key in ("selector", "field", "operator")):
        return "check_text"
    operator, expected = check["operator"], check["expected"]
    if operator in {"contains_any", "contains_all"} and not _scalar_list(expected):
        return "check_expected_scalar_list"
    if operator in {"scalar_gt", "any_gt"} and type(expected) not in {int, float}:
        return "check_expected_number"
    if operator in {"nonempty", "exists"} and not isinstance(expected, bool):
        return "check_expected_bool"
    if operator in {"scalar_equals", "all_equal"} and not _is_scalar(expected):
        return "check_expected_scalar"
    return None


def score(
    checks: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    tool_results: list[dict[str, Any]] | None = None,
    scope: dict[str, str],
) -> dict[str, Any]:
    failures: list[str] = []
    evidence_errors: list[str] = []
    required_scope = {"shopping_session_id", "run_id"}
    if set(scope) != required_scope or not all(isinstance(scope[key], str) and scope[key]
                                               for key in required_scope):
        return {"status": "evidence_invalid", "passed": False,
                "evidence_errors": ["invalid_or_missing_scope"], "failures": []}
    scoped_events = [event for event in events if _in_scope(event, scope)]
    scoped_results = [result for result in (tool_results or []) if _in_scope(result, scope)]
    invoked_tools = [event.get("payload", {}).get("tool") for event in scoped_events
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
        elif check["kind"] == "tool_result":
            payloads = [result["payload"] for result in scoped_results
                        if result.get("selector") == check["selector"]
                        and isinstance(result.get("payload"), dict)]
            if not payloads:
                failures.append(f"check[{index}]:selector_not_observed")
                continue
            actual = _project(payloads[-1], check["field"])
        else:
            payloads = _event_payloads(scoped_events, check["selector"])
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
