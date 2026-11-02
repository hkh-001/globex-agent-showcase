"""Contracts for the zero-model scorer-plumbing replay."""
from __future__ import annotations

import hashlib
import json

import pytest

from scripts.eval.agentbench import run_pilot_plumbing, scorer


@pytest.mark.asyncio
async def test_plumbing_replay_runs_real_tools_and_both_scorers(tmp_path):
    output, network, manifest = (tmp_path / name for name in ("evidence.json", "network.jsonl", "manifest.json"))
    payload = await run_pilot_plumbing.run(output, network)
    recorded = run_pilot_plumbing.write_manifest(output, network, manifest, payload)
    assert payload["mode"] == "scorer_plumbing_fixture_replay"
    assert payload["agent_executed"] is False
    assert payload["task_count"] == payload["passed_count"] == 3
    assert payload["external_connect_attempts"] == payload["paid_requests"] == 0
    assert all(row["execution_score"]["passed"] and row["deterministic_score"]["passed"]
               for row in payload["records"])
    assert all(row["physical_model_calls"] == 0 for row in payload["records"])
    assert recorded["evidence_file_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert recorded["records_sha256"] == payload["records_sha256"]

    knowledge = next(row for row in payload["records"] if "knowledge" in row["task_id"])
    event = next(event for event in knowledge["events"]
                 if event["type"] == "tool.result" and event["payload"]["tool"] == "category_insight_tool")
    assert "insights" not in event["payload"]
    result = next(item for item in knowledge["tool_results"]
                  if item["selector"] == "category_insight_tool")
    assert any(item["source"] == "eval-digital-accessories-价格与预算.md"
               for item in result["payload"]["insights"])


@pytest.mark.asyncio
async def test_plumbing_replay_enforces_declared_budgets():
    pilot = json.loads(run_pilot_plumbing.PILOT.read_text(encoding="utf-8"))
    too_many_turns = {**pilot["tasks"][0], "max_turns": 0}
    too_many_calls = {**pilot["tasks"][0], "max_model_calls": -1}
    assert (await run_pilot_plumbing._execute(too_many_turns))["failure"] == "max_turns"
    assert (await run_pilot_plumbing._execute(too_many_calls))["failure"] == "max_model_calls"


def test_execution_scorer_scope_excludes_foreign_events():
    contract = {
        "allowed_agents": ["main_agent"],
        "allowed_edges": [["main_agent", "product_search_tool"]],
        "forbidden_edges": [["main_agent", "web_search_tool"]],
        "required_capabilities": ["search.catalog"],
    }
    events = [
        {"shopping_session_id": "wanted", "type": "tool.invoke",
         "payload": {"tool": "product_search_tool"},
         "correlation": {"agent": "main_agent", "run_id": "run-1"}},
        {"shopping_session_id": "other", "type": "tool.invoke",
         "payload": {"tool": "web_search_tool"},
         "correlation": {"agent": "main_agent", "run_id": "run-2"}},
    ]
    scoped = scorer.score_execution_graph(
        events, contract, scope={"shopping_session_id": "wanted", "run_id": "run-1"})
    assert scoped["passed"]
    assert not scorer.score_execution_graph(events, contract)["passed"]


def test_plumbing_replay_refuses_enabled_pilot_flags(tmp_path, monkeypatch):
    pilot = json.loads(run_pilot_plumbing.PILOT.read_text(encoding="utf-8"))
    pilot["tasks"][0]["automatic_scoring"] = True
    changed = tmp_path / "pilot.json"
    changed.write_text(json.dumps(pilot), encoding="utf-8")
    monkeypatch.setattr(run_pilot_plumbing, "PILOT", changed)
    with pytest.raises(ValueError, match="flags"):
        import asyncio
        asyncio.run(run_pilot_plumbing.run(tmp_path / "out.json", tmp_path / "network.jsonl"))
