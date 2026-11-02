"""Zero-paid preflight and executor-contract tests (no model calls)."""
from __future__ import annotations

import asyncio
import json
import pathlib

import pytest

from scripts.eval.agentbench import run_pilot_agent

ROOT = pathlib.Path(__file__).resolve().parents[1]
PILOT = ROOT / "stage2/pilot/pilot-candidates.json"


class _FakeBackend:
    def __init__(self, calls=1):
        self.calls = calls

    async def run(self, task, scope, ledger):
        for _ in range(self.calls):
            ledger.reserve(task["task_id"], task["max_model_calls"])
        events = [
            {"shopping_session_id": scope["shopping_session_id"], "type": "tool.invoke",
             "payload": {"tool": "product_search_tool"},
             "correlation": {"agent": "main_agent", "run_id": scope["run_id"]}},
            {"shopping_session_id": scope["shopping_session_id"], "type": "tool.result",
             "payload": {"tool": "product_search_tool", "hits": [{
                 "product_id": "P1015", "canonical_product_id": "CAN-LEGACY-014",
                 "currency": "CNY", "skus": [{"stock": 3}]}]},
             "correlation": {"agent": "main_agent", "run_id": scope["run_id"]}},
        ]
        return {"events": events, "tool_results": [], "agent_executed": True,
                "physical_model_calls": self.calls}


def test_preflight_is_zero_paid_and_keeps_flags_false():
    report = run_pilot_agent.preflight()
    assert report["passed"] is True
    assert report["task_count"] == 3
    assert report["paid_requests"] == 0
    assert report["physical_model_calls"] == 0
    assert report["agent_executed"] is False
    assert report["automatic_scoring"] is False
    assert report["execution_ready"] is False


def test_execute_requires_explicit_paid_authorization():
    task = json.loads(PILOT.read_text(encoding="utf-8"))["tasks"][0]
    with pytest.raises(ValueError):
        asyncio.run(run_pilot_agent.execute([task], _FakeBackend(), {}))


def test_execute_budget_and_flags_are_fail_closed():
    task = json.loads(PILOT.read_text(encoding="utf-8"))["tasks"][0]
    result = asyncio.run(run_pilot_agent.execute(
        [task], _FakeBackend(calls=1),
        {"paid_allowed": True, "max_total_model_calls": 1}))
    assert result["model_calls"] == 1
    assert result["automatic_scoring"] is False
    assert result["execution_ready"] is False
    with pytest.raises(ValueError):
        asyncio.run(run_pilot_agent.execute(
            [task], _FakeBackend(calls=2),
            {"paid_allowed": True, "max_total_model_calls": 1}))


class _GreedyBackend:
    def __init__(self, attempts):
        self.attempts = attempts
        self.dispatched = 0

    async def run(self, task, scope, ledger):
        for _ in range(self.attempts):
            ledger.reserve(task["task_id"], task["max_model_calls"])
            self.dispatched += 1
        return {"events": [], "tool_results": [], "agent_executed": True,
                "physical_model_calls": self.attempts}


def test_reservation_blocks_before_dispatch_and_per_task_cap():
    task = json.loads(PILOT.read_text(encoding="utf-8"))["tasks"][0]
    task["max_model_calls"] = 1
    backend = _GreedyBackend(attempts=2)
    with pytest.raises(run_pilot_agent.BudgetExceeded):
        asyncio.run(run_pilot_agent.execute(
            [task], backend, {"paid_allowed": True, "max_total_model_calls": 5}))
    assert backend.dispatched == 1


def test_backend_report_must_match_ledger():
    class _Lying(_FakeBackend):
        async def run(self, task, scope, ledger):
            ledger.reserve(task["task_id"], task["max_model_calls"])
            return {"events": [], "tool_results": [], "agent_executed": True,
                    "physical_model_calls": 2}
    task = json.loads(PILOT.read_text(encoding="utf-8"))["tasks"][0]
    with pytest.raises(ValueError, match="ledger"):
        asyncio.run(run_pilot_agent.execute(
            [task], _Lying(), {"paid_allowed": True, "max_total_model_calls": 5}))


def test_execute_revalidates_flags_and_ids():
    task = json.loads(PILOT.read_text(encoding="utf-8"))["tasks"][0]
    with pytest.raises(ValueError, match="flags"):
        asyncio.run(run_pilot_agent.execute(
            [{**task, "execution_ready": True}], _FakeBackend(),
            {"paid_allowed": True, "max_total_model_calls": 5}))
    with pytest.raises(ValueError, match="duplicate"):
        asyncio.run(run_pilot_agent.execute(
            [task, task], _FakeBackend(), {"paid_allowed": True, "max_total_model_calls": 5}))
