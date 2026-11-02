"""Zero-paid preflight and executor contract for real Agent pilots.

No backend is wired here.  `preflight` never imports or calls a model; `execute`
refuses to run until an explicit canary authorization file exists and a real
Agent backend is supplied.  Both pilot flags stay false.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import threading
import json
from pathlib import Path
from typing import Any, Protocol

from scripts.eval.agentbench import deterministic_checks, scorer

ROOT = Path(__file__).resolve().parents[3]
PILOT = ROOT / "stage2/pilot/pilot-candidates.json"
PREFLIGHT = ROOT / "stage2/pilot/agent-preflight.json"


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


class BudgetExceeded(ValueError):
    pass


class ModelCallLedger:
    """Atomic pre-dispatch reservations for one executor run."""

    def __init__(self, global_limit: int):
        if not isinstance(global_limit, int) or global_limit <= 0:
            raise ValueError("global model-call budget must be positive")
        self._global_limit = global_limit
        self._total = 0
        self._per_task: dict[str, int] = {}
        self._lock = threading.Lock()

    def reserve(self, task_id: str, task_limit: int) -> int:
        if not isinstance(task_limit, int) or task_limit <= 0:
            raise BudgetExceeded(f"{task_id}: invalid task budget")
        with self._lock:
            used = self._per_task.get(task_id, 0)
            if used + 1 > task_limit:
                raise BudgetExceeded(f"{task_id}: per-task budget exceeded")
            if self._total + 1 > self._global_limit:
                raise BudgetExceeded("global budget exceeded")
            self._per_task[task_id] = used + 1
            self._total += 1
            return self._total

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"total": self._total, "per_task": dict(self._per_task),
                    "global_limit": self._global_limit}


class AgentBackend(Protocol):
    async def run(self, task: dict[str, Any], scope: dict[str, str],
                  ledger: ModelCallLedger) -> dict[str, Any]:
        """Must call ledger.reserve(task_id, task['max_model_calls']) before each model request."""


def preflight(pilot_path: Path = PILOT) -> dict[str, Any]:
    pilot = json.loads(pilot_path.read_text(encoding="utf-8"))
    tasks = pilot.get("tasks") or []
    errors = []
    if len(tasks) != 3:
        errors.append("pilot must contain exactly three tasks")
    if len({task.get("task_id") for task in tasks}) != len(tasks):
        errors.append("duplicate task_id")
    for task in tasks:
        if task.get("automatic_scoring") is not False or task.get("execution_ready") is not False:
            errors.append(f"{task.get('task_id')}: flags must stay false")
        if not task.get("turns") or not task.get("deterministic_checks"):
            errors.append(f"{task.get('task_id')}: incomplete task contract")
    report = {
        "schema_version": "agentbench-agent-preflight-v1",
        "mode": "preflight_only",
        "pilot_sha256": hashlib.sha256(pilot_path.read_bytes()).hexdigest(),
        "task_count": len(tasks),
        "paid_requests": 0,
        "agent_executed": False,
        "physical_model_calls": 0,
        "automatic_scoring": False,
        "execution_ready": False,
        "errors": errors,
        "passed": not errors,
    }
    return report


async def execute(tasks: list[dict[str, Any]], backend: AgentBackend,
                  authorization: dict[str, Any]) -> dict[str, Any]:
    if authorization.get("paid_allowed") is not True:
        raise ValueError("paid execution is not authorized")
    budget = authorization.get("max_total_model_calls")
    if not isinstance(budget, int) or budget <= 0:
        raise ValueError("authorization must declare a positive model-call budget")
    if len({task.get("task_id") for task in tasks}) != len(tasks):
        raise ValueError("duplicate task_id")
    for task in tasks:
        if task.get("automatic_scoring") is not False or task.get("execution_ready") is not False:
            raise ValueError(f"{task.get('task_id')}: flags must stay false")
        if not task.get("turns") or not task.get("deterministic_checks"):
            raise ValueError(f"{task.get('task_id')}: incomplete task contract")
    ledger = ModelCallLedger(budget)
    records = []
    spent = 0
    for index, task in enumerate(tasks):
        scope = {"shopping_session_id": f"agent-{task['task_id']}-session",
                 "run_id": f"agent-{task['task_id']}-run-{index}"}
        result = await backend.run(task, scope, ledger)
        calls = result.get("physical_model_calls")
        if result.get("agent_executed") is not True or not isinstance(calls, int) or calls < 0:
            raise ValueError(f"{task['task_id']}: backend must report real agent execution and calls")
        task_spent = ledger.snapshot()["per_task"].get(task["task_id"], 0)
        if calls != task_spent:
            raise ValueError(f"{task['task_id']}: backend calls {calls} != ledger {task_spent}")
        if calls > task["max_model_calls"]:
            raise ValueError(f"{task['task_id']}: per-task budget exceeded")
        spent += calls
        events = result.get("events") or []
        tool_results = result.get("tool_results") or []
        execution = scorer.score_execution_graph(events, task, scope=scope)
        oracle = deterministic_checks.score(task["deterministic_checks"], events,
                                            tool_results=tool_results, scope=scope)
        records.append({
            "task_id": task["task_id"], "scope": scope,
            "model_calls": calls, "events": events, "tool_results": tool_results,
            "execution_score": execution, "deterministic_score": oracle,
            "passed": execution["passed"] and oracle["passed"],
        })
    return {
        "schema_version": "agentbench-agent-execution-v1",
        "mode": "real_agent_canary",
        "tasks": len(records), "passed_count": sum(record["passed"] for record in records),
        "model_calls": spent,
        "ledger": ledger.snapshot(),
        "records": records,
        "records_sha256": _sha(records),
        "automatic_scoring": False,
        "execution_ready": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["preflight", "execute"], default="preflight")
    parser.add_argument("--authorization", type=Path, default=None)
    parser.add_argument("--out", type=Path, default=PREFLIGHT)
    args = parser.parse_args()
    if args.mode == "preflight":
        report = preflight()
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        raise SystemExit(0 if report["passed"] else 1)
    raise SystemExit("execute requires a real Agent backend and an approved canary budget; "
                     "the CLI cannot make model calls by itself")


if __name__ == "__main__":
    main()
