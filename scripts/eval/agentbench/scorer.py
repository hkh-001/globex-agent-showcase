"""Fail-closed execution-graph scorer for AgentBench traces."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any, Iterable


DEFAULT_CAPABILITIES = {
    "search.catalog": {"product_search_tool"},
    "search.category_insight": {"category_insight_tool"},
    "dispatch.search": {"product_search_tool"},
    "catalog_search": {"product_search_tool"},
    "category_knowledge": {"category_insight_tool"},
    "order_create": {"create_order_tool"},
    "order_query": {"query_order_tool"},
    "order_cancel": {"cancel_order_tool"},
}


def summarize_execution(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Count observable turns/calls without treating missing usage as zero."""
    model_calls = 0
    turns = 0
    usage_complete = True
    tools = 0
    dispatch_ids: set[str] = set()
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        if event.get("type") == "usage.summary":
            turns += 1
            value = payload.get("model_calls")
            if type(value) is not int or value < 0:
                usage_complete = False
            else:
                model_calls += value
            usage_complete = usage_complete and payload.get("usage_complete") is True
        elif event.get("type") == "tool.invoke":
            tools += 1
        elif event.get("type") == "agent.dispatch":
            correlation = event.get("correlation") or {}
            identity = correlation.get("dispatch_id") or payload.get("dispatch_id")
            if isinstance(identity, str) and identity:
                dispatch_ids.add(identity)
    if turns == 0:
        usage_complete = False
    return {
        "turns": turns,
        "physical_model_calls": model_calls if usage_complete else None,
        "usage_complete": usage_complete,
        "tool_calls": tools,
        "dispatches": len(dispatch_ids),
    }


def _edge(source: str, target: str) -> tuple[str, str]:
    return source, target


def score_execution_graph(
    events: Iterable[dict[str, Any]],
    contract: dict[str, Any],
    *,
    capability_map: dict[str, set[str]] | None = None,
) -> dict[str, Any]:
    """Score observable execution only; business-oracle checks are separate.

    Missing ownership is evidence_invalid rather than a model failure.  This
    prevents old or truncated journals from silently passing a route check.
    """
    capabilities = capability_map or DEFAULT_CAPABILITIES
    failures: list[str] = []
    evidence_errors: list[str] = []
    edges: list[tuple[str, str]] = []
    dispatches: dict[str, tuple[str, str]] = {}
    completed_dispatches: dict[str, str] = {}
    child_dispatches: dict[str, set[str]] = defaultdict(set)

    for index, event in enumerate(events):
        if event.get("type") not in {"tool.invoke", "tool.result", "agent.dispatch"}:
            continue
        payload = event.get("payload")
        correlation = event.get("correlation")
        if not isinstance(payload, dict) or not isinstance(correlation, dict):
            evidence_errors.append(f"event[{index}]:missing_payload_or_correlation")
            continue
        agent = correlation.get("agent")
        if not isinstance(agent, str) or not agent:
            evidence_errors.append(f"event[{index}]:missing_agent")
            continue

        if event["type"] == "agent.dispatch":
            target = payload.get("agent")
            dispatch_id = correlation.get("dispatch_id") or payload.get("dispatch_id")
            if not isinstance(target, str) or not target:
                evidence_errors.append(f"event[{index}]:missing_dispatch_target")
                continue
            if not isinstance(dispatch_id, str) or not dispatch_id:
                evidence_errors.append(f"event[{index}]:missing_dispatch_id")
                continue
            if dispatch_id in dispatches:
                evidence_errors.append(f"event[{index}]:duplicate_dispatch_id")
                continue
            dispatches[dispatch_id] = (agent, target)
            edges.append(_edge(agent, target))
            continue

        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool:
            evidence_errors.append(f"event[{index}]:missing_tool")
            continue
        if agent not in {"main_agent", "system"}:
            dispatch_id = correlation.get("dispatch_id")
            if not isinstance(dispatch_id, str) or not dispatch_id:
                evidence_errors.append(f"event[{index}]:child_missing_dispatch_id")
                continue
            child_dispatches[agent].add(dispatch_id)
        if event["type"] == "tool.invoke":
            edges.append(_edge(agent, tool))
        elif tool == "task_dispatch":
            dispatch_id = correlation.get("dispatch_id") or payload.get("dispatch_id")
            if not isinstance(dispatch_id, str) or not dispatch_id:
                evidence_errors.append(f"event[{index}]:dispatch_result_missing_id")
            elif payload.get("status") not in {"completed", "error", "cancelled"}:
                evidence_errors.append(f"event[{index}]:dispatch_result_missing_status")
            elif dispatch_id in completed_dispatches:
                evidence_errors.append(f"event[{index}]:duplicate_dispatch_result")
            else:
                completed_dispatches[dispatch_id] = payload["status"]

    if set(dispatches) != set(completed_dispatches):
        evidence_errors.append("dispatch_start_finish_mismatch")

    if evidence_errors:
        return {
            "status": "evidence_invalid",
            "passed": False,
            "evidence_errors": evidence_errors,
            "failures": [],
            "edges": [list(edge) for edge in edges],
        }

    allowed_agents = set(contract.get("allowed_agents", []))
    for source, target in edges:
        if allowed_agents and source not in allowed_agents:
            failures.append(f"agent_not_allowed:{source}")

    allowed_edges = {tuple(edge) for edge in contract.get("allowed_edges", [])}
    forbidden_edges = {tuple(edge) for edge in contract.get("forbidden_edges", [])}
    for edge in edges:
        if edge in forbidden_edges:
            failures.append("forbidden_edge:" + "->".join(edge))
        if allowed_edges and edge not in allowed_edges:
            failures.append("edge_not_allowed:" + "->".join(edge))

    invoked_tools = {target for _, target in edges if target.endswith("_tool")}
    for capability in contract.get("required_capabilities", []):
        tools = capabilities.get(capability)
        if not tools:
            failures.append(f"unknown_capability:{capability}")
        elif not invoked_tools.intersection(tools):
            failures.append(f"missing_capability:{capability}")

    policy = contract.get("dispatch_policy", {})
    count = len(dispatches)
    if policy.get("required") and count == 0:
        failures.append("dispatch_required")
    maximum = policy.get("max_dispatches")
    if isinstance(maximum, int) and count > maximum:
        failures.append(f"too_many_dispatches:{count}>{maximum}")
    for dispatch_id, status in completed_dispatches.items():
        if status != "completed":
            failures.append(f"dispatch_{status}:{dispatch_id}")

    # A group lists child-agent names which must have distinct dispatches.
    # This proves distinct dispatch ownership, not wall-clock overlap.
    for group_index, group in enumerate(contract.get("dispatch_isolation_groups", [])):
        required = Counter(group)
        if any(len(child_dispatches.get(agent, set())) < count for agent, count in required.items()):
            failures.append(f"parallel_group_not_isolated:{group_index}")

    return {
        "status": "passed" if not failures else "task_failed",
        "passed": not failures,
        "evidence_errors": [],
        "failures": sorted(set(failures)),
        "edges": [list(edge) for edge in edges],
        "dispatch_count": count,
        "tool_invocations": dict(Counter(target for _, target in edges if target.endswith("_tool"))),
    }
