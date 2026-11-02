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

    async def run(self, task, scope):
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
