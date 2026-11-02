"""Contract tests for the deterministic Stage 2 pilot candidates."""
from __future__ import annotations

import json
import pathlib

import pytest

from app.application.tools.category_insight_tool import build_category_insight_tool
from app.infrastructure.eventbus import TradeEventBus, observe_run_events
from scripts.eval.agentbench import deterministic_checks, generate_pilot, scorer, validate_pilot

ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG = ROOT / "eval/agent/v1/template-catalog.json"
MATRIX = ROOT / "eval/agent/v1/capability-matrix.json"
PILOT = ROOT / "stage2/pilot/pilot-candidates.json"


def test_generation_is_deterministic():
    assert generate_pilot.build() == generate_pilot.build()


def test_committed_pilot_matches_generator():
    assert generate_pilot.build() == json.loads(PILOT.read_text(encoding="utf-8"))


def test_pilot_is_small_and_dev_only():
    data = json.loads(PILOT.read_text(encoding="utf-8"))
    assert 1 <= data["candidate_count"] <= 6 < 60
    assert data["formal_eval_size"] is False
    assert data["holdout_used"] is False
    tasks = data["tasks"]
    assert all(task["split"] == "dev" for task in tasks)
    assert len({task["leakage_group_id"] for task in tasks}) == len(tasks)


def test_every_group_is_a_dev_catalog_group():
    data = json.loads(PILOT.read_text(encoding="utf-8"))
    catalog = {item["leakage_group_id"]: item for item in
               json.loads(CATALOG.read_text(encoding="utf-8"))["templates"]}
    for task in data["tasks"]:
        group = catalog[task["leakage_group_id"]]
        assert group["evaluation_split"] == "dev"
        assert group["module"] == task["module"]


def test_capabilities_are_verified_and_isolated():
    data = json.loads(PILOT.read_text(encoding="utf-8"))
    verified = {item["capability_id"] for item in
                json.loads(MATRIX.read_text(encoding="utf-8"))["capabilities"]
                if item.get("verification_status") == "verified"}
    for task in data["tasks"]:
        assert task["required_capabilities"]
        assert set(task["required_capabilities"]) <= verified
        assert task["deterministic_checks"]
        assert task["isolation"]["database"] == "fresh_per_task"
        assert task["turns"] and task["source_record_id"] and task["source_prompt_sha256"]
        assert task["automatic_scoring"] is False
        assert task["execution_ready"] is False
        assert validate_pilot.validate(PILOT) == []
        assert task["side_effect_policy"]["before_after_snapshot"] in {"none", "trade_repository"}
        assert task["max_turns"] >= 1 and task["max_model_calls"] >= 0


def test_source_oracles_are_inherited():
    data = json.loads(PILOT.read_text(encoding="utf-8"))
    search = next(task for task in data["tasks"] if task["capability"] == "search.catalog")
    assert "P1015" in search["source_oracle"]["relevant"]
    assert search["source_oracle"]["relevant_canonical_ids"] == ["CAN-LEGACY-014"]
    assert search["source_oracle"]["require_in_stock"] is True
    routing = next(task for task in data["tasks"] if task["capability"] == "dispatch.search")
    assert "product_search_tool" in {step for plan in routing["acceptable_execution_plans"] for step in plan}
    assert routing["source_oracle"]["required_tools"] == ["product_search_tool"]


def _write_tampered(tmp_path, mutate):
    data = json.loads(PILOT.read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return validate_pilot.validate(path)


def test_validator_rejects_adapter_and_oracle_mismatch(tmp_path):
    mutations = [
        lambda data: data["tasks"][0].__setitem__("oracle_adapter_id", "evil"),
        lambda data: data["tasks"][0].__setitem__("oracle_adapter_version", "v999"),
        lambda data: data["tasks"][0].__setitem__("source_oracle", {"relevant": ["EVIL"]}),
        lambda data: data["tasks"][0]["turns"][0].__setitem__("content", "tampered"),
    ]
    for mutate in mutations:
        assert _write_tampered(tmp_path, mutate)


def test_validator_rejects_weakened_execution_contract(tmp_path):
    mutations = [
        lambda data: data["tasks"][0].__setitem__("required_capabilities", []),
        lambda data: data["tasks"][0].__setitem__("allowed_edges", []),
        lambda data: data["tasks"][0].__setitem__("allowed_agents", []),
        lambda data: data["tasks"][0].__setitem__("forbidden_edges", []),
        lambda data: data["tasks"][0].__setitem__("max_model_calls", 999),
        lambda data: data["tasks"][0].__setitem__("execution_ready", True),
    ]
    for mutate in mutations:
        assert _write_tampered(tmp_path, mutate)


def _event(event_type, tool, payload=None, *, agent="main_agent", dispatch_id=None,
           session="pilot-session", run_id="pilot-run"):
    body = {"tool": tool, **(payload or {})}
    correlation = {"agent": agent}
    if dispatch_id:
        correlation["dispatch_id"] = dispatch_id
    correlation["run_id"] = run_id
    return {"shopping_session_id": session, "type": event_type, "payload": body,
            "correlation": correlation}


def _positive_traces():
    search = [
        _event("tool.invoke", "product_search_tool"),
        _event("tool.result", "product_search_tool", {"hits": [{
            "product_id": "P1015", "canonical_product_id": "CAN-LEGACY-014",
            "landed_price": {"currency": "CNY"}, "skus": [{"stock": 3}],
        }]}),
    ]
    knowledge = [
        _event("tool.invoke", "category_insight_tool"),
        _event("tool.result", "category_insight_tool", {
            "hit_count": 1, "policy_fact_statuses": ["current"],
            "insights": [{"source": "eval-digital-accessories-价格与预算.md"}],
        }),
    ]
    routing = [
        {"shopping_session_id": "pilot-session", "type": "agent.dispatch",
         "payload": {"agent": "search_agent", "dispatch_id": "d1"},
         "correlation": {"agent": "main_agent", "dispatch_id": "d1", "run_id": "pilot-run"}},
        _event("tool.invoke", "product_search_tool", agent="search_agent", dispatch_id="d1"),
        _event("tool.result", "product_search_tool", {"hits": [{"product_id": "P1"}]},
               agent="search_agent", dispatch_id="d1"),
        _event("tool.result", "task_dispatch", {"agent": "search_agent", "dispatch_id": "d1",
               "status": "completed"}, dispatch_id="d1"),
    ]
    return {"search.catalog": search, "search.category_insight": knowledge, "dispatch.search": routing}


def test_three_pilot_contracts_accept_positive_and_reject_negative_traces():
    data = json.loads(PILOT.read_text(encoding="utf-8"))
    traces = _positive_traces()
    for task in data["tasks"]:
        events = traces[task["capability"]]
        tool_results = ([{"selector": "category_insight_tool", "shopping_session_id": "pilot-session",
                          "run_id": "pilot-run", "payload": {
                              "insights": [{"source": "eval-digital-accessories-价格与预算.md"}]}}]
                        if task["capability"] == "search.category_insight" else [])
        assert scorer.score_execution_graph(events, task)["passed"]
        assert deterministic_checks.score(task["deterministic_checks"], events,
                                            tool_results=tool_results)["passed"]
        broken = [event for event in events if event.get("payload", {}).get("tool") != (
            "product_search_tool" if task["capability"] != "search.category_insight" else "category_insight_tool")]
        assert not deterministic_checks.score(task["deterministic_checks"], broken,
                                                tool_results=tool_results)["passed"]


def test_deterministic_check_schema_and_array_quantifiers_are_fail_closed():
    assert deterministic_checks.validate_check({"kind": "event", "selector": "x:y", "field": "a",
                                                 "operator": "included", "expected": []}) == "check_operator"
    checks = [{"kind": "event", "selector": "tool.result:x", "field": "hits[].id",
               "operator": "contains_all", "expected": ["A", "B"]}]
    events = [_event("tool.result", "x", {"hits": [{"id": "A"}]})]
    assert not deterministic_checks.score(checks, events)["passed"]


def test_last_matching_observation_is_authoritative():
    checks = [{"kind": "event", "selector": "tool.result:x", "field": "hits[].id",
               "operator": "contains_any", "expected": ["GOOD"]}]
    events = [
        _event("tool.result", "x", {"hits": [{"id": "GOOD"}]}),
        _event("tool.result", "x", {"hits": [{"id": "BAD"}]}),
    ]
    assert not deterministic_checks.score(checks, events)["passed"]


@pytest.mark.asyncio
async def test_knowledge_oracle_uses_real_tool_chunk_not_bus_payload(tmp_path):
    class BrokenKnowledgeBase:
        async def search(self, *args, **kwargs):
            raise RuntimeError("force deterministic fallback")

    docs = tmp_path / "knowledge"
    docs.mkdir()
    filename = "eval-digital-accessories-价格与预算.md"
    (docs / filename).write_text(
        "# 数码配件价格与预算\n选购数码配件时核对商品价格、运费、税费与币种。", encoding="utf-8")
    bus = TradeEventBus()
    tool = build_category_insight_tool(BrokenKnowledgeBase(), bus, fallback_knowledge_dir=docs)
    captured = []
    with observe_run_events(captured.append):
        chunk = await tool(question="选购数码配件价格与预算时核对哪些字段", top_k=2)
    events = [event.to_dict() for event in captured]
    event_result = next(event for event in events if event["type"] == "tool.result")
    assert "insights" not in event_result["payload"]
    payload = json.loads(chunk.content[0].text)
    tool_results = [{"selector": "category_insight_tool", "payload": payload}]
    task = next(task for task in json.loads(PILOT.read_text())["tasks"]
                if task["capability"] == "search.category_insight")
    assert deterministic_checks.score(task["deterministic_checks"], events,
                                       tool_results=tool_results)["passed"]
    assert not deterministic_checks.score(task["deterministic_checks"], events,
                                           tool_results=[])["passed"]


def test_check_types_fail_closed_without_type_errors():
    bad = [
        {"kind": "event", "selector": "tool.result:x", "field": "a",
         "operator": "contains_any", "expected": [{"not": "scalar"}]},
        {"kind": "event", "selector": "tool.result:x", "field": "a",
         "operator": "any_gt", "expected": "zero"},
        {"kind": "event", "selector": "tool.result:x", "field": "a",
         "operator": "nonempty", "expected": "false"},
    ]
    for check in bad:
        result = deterministic_checks.score([check], [_event("tool.result", "x", {"a": [{}]})])
        assert result["status"] == "evidence_invalid"


def test_scope_excludes_other_session_and_run():
    check = {"kind": "event", "selector": "tool.result:x", "field": "value",
             "operator": "scalar_equals", "expected": "GOOD"}
    events = [
        _event("tool.result", "x", {"value": "GOOD"}, session="wanted", run_id="run-1"),
        _event("tool.result", "x", {"value": "BAD"}, session="other", run_id="run-2"),
    ]
    assert deterministic_checks.score([check], events,
                                       scope={"shopping_session_id": "wanted", "run_id": "run-1"})["passed"]
    assert not deterministic_checks.score([check], events,
                                           scope={"shopping_session_id": "other", "run_id": "run-2"})["passed"]
