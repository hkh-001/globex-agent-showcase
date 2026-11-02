"""Zero-model scorer-plumbing replay for the three Stage 2 pilot drafts.

This exercises real tools and captures their real bus events / ToolChunks.  It
does not execute an LLM Agent and must never be reported as task success rate.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

from app.application.tools.category_insight_tool import build_category_insight_tool
from app.application.tools.product_search_tool import build_product_search_tool
from app.application.tools.task_dispatch_tool import build_task_dispatch_tool
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.infrastructure.agent_execution import agent_execution_scope
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEventBus, observe_run_events
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.rag.category_knowledge import KNOWLEDGE_DIR
from scripts.eval.agentbench import deterministic_checks, network_guard, scorer

ROOT = Path(__file__).resolve().parents[3]
PILOT = ROOT / "stage2/pilot/pilot-candidates.json"


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=str).encode()).hexdigest()


class _EvidenceStore:
    async def save(self, *_args, **_kwargs):
        return "plumbing-result"


def _chunk_payload(chunk) -> dict:
    text = "".join(getattr(block, "text", "") for block in chunk.content)
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("tool result must be a JSON object")
    return value


def _event_record(event, run_id: str) -> dict:
    correlation = dict(getattr(event, "correlation", {}) or {})
    correlation["run_id"] = run_id
    return {
        "shopping_session_id": event.shopping_session_id,
        "type": event.type,
        "payload": event.payload,
        "occurred_at": event.occurred_at,
        "correlation": correlation,
    }


def _tool_record(selector: str, chunk, scope: dict[str, str]) -> dict:
    return {"selector": selector, "payload": _chunk_payload(chunk), **scope}


class _Factory:
    def __init__(self, worker):
        self.worker = worker

    def build(self):
        return self.worker


class _SearchFixtureWorker:
    def __init__(self, search_tool, tool_results: list[dict], scope: dict[str, str]):
        self.search_tool, self.tool_results, self.scope = search_tool, tool_results, scope

    async def reply(self, inputs):
        demand = inputs[-1].get_text_content()
        chunk = await self.search_tool(normalized_query=demand, ship_to="JP", top_k=5)
        self.tool_results.append(_tool_record("product_search_tool", chunk, self.scope))
        return SimpleNamespace(get_text_content=lambda: "fixture search completed")


class _UnavailableWorker:
    async def reply(self, _inputs):
        raise AssertionError("unexpected trade worker dispatch")


class _BrokenKnowledgeBase:
    async def search(self, *_args, **_kwargs):
        raise RuntimeError("fixture replay uses deterministic knowledge fallback")


@contextmanager
def _blocked_external_network(log_path: Path):
    previous = os.environ.get("AGENTBENCH_NETWORK_GUARD_LOG")
    os.environ["AGENTBENCH_NETWORK_GUARD_LOG"] = str(log_path)
    network_guard.pytest_configure(None)
    try:
        yield
    finally:
        network_guard.pytest_unconfigure(None)
        if previous is None:
            os.environ.pop("AGENTBENCH_NETWORK_GUARD_LOG", None)
        else:
            os.environ["AGENTBENCH_NETWORK_GUARD_LOG"] = previous


async def _execute(task: dict) -> dict:
    session_id = f"plumbing-{task['task_id']}"
    run_id = f"run-{task['task_id']}"
    scope = {"shopping_session_id": session_id, "run_id": run_id}
    if len(task["turns"]) > task["max_turns"]:
        return {"task_id": task["task_id"], "status": "task_failed", "failure": "max_turns"}
    model_calls = 0
    if model_calls > task["max_model_calls"]:
        return {"task_id": task["task_id"], "status": "task_failed", "failure": "max_model_calls"}

    bus, captured, tool_results = TradeEventBus(), [], []
    search_tool = build_product_search_tool(
        CatalogSearchUseCase(InMemoryProductRepository()), bus, _EvidenceStore())
    context_token = ShoppingContext.set(ShoppingContextSnapshot(
        session_id, task["isolation"]["buyer_id"], "zh-CN", "CNY"))
    try:
        with agent_execution_scope("main_agent"), observe_run_events(captured.append):
            if task["capability"] == "search.catalog":
                chunk = await search_tool(**task["expected_tool_parameters"])
                tool_results.append(_tool_record("product_search_tool", chunk, scope))
            elif task["capability"] == "search.category_insight":
                tool = build_category_insight_tool(
                    _BrokenKnowledgeBase(), bus, fallback_knowledge_dir=KNOWLEDGE_DIR)
                chunk = await tool(**task["expected_tool_parameters"])
                tool_results.append(_tool_record("category_insight_tool", chunk, scope))
            elif task["capability"] == "dispatch.search":
                worker = _SearchFixtureWorker(search_tool, tool_results, scope)
                dispatch = build_task_dispatch_tool(
                    _Factory(worker), _Factory(_UnavailableWorker()), bus, subagent_inject=False)
                chunk = await dispatch("search_agent", task["expected_tool_parameters"]["demands"])
                tool_results.append(_tool_record("task_dispatch", chunk, scope))
            else:
                raise ValueError(f"unsupported plumbing capability: {task['capability']}")
    finally:
        ShoppingContext.reset(context_token)

    events = [_event_record(event, run_id) for event in captured]
    execution = scorer.score_execution_graph(events, task, scope=scope)
    oracle = deterministic_checks.score(
        task["deterministic_checks"], events, tool_results=tool_results, scope=scope)
    passed = execution["passed"] and oracle["passed"]
    return {
        "task_id": task["task_id"], "mode": "scorer_plumbing_fixture_replay",
        "agent_executed": False, "scope": scope, "turns": len(task["turns"]),
        "physical_model_calls": model_calls, "events": events, "tool_results": tool_results,
        "events_sha256": _sha(events), "tool_results_sha256": _sha(tool_results),
        "execution_score": execution, "deterministic_score": oracle,
        "status": "passed" if passed else "task_failed", "passed": passed,
    }


async def run(output: Path, network_log: Path) -> dict:
    pilot = json.loads(PILOT.read_text(encoding="utf-8"))
    if any(task.get("automatic_scoring") is not False or task.get("execution_ready") is not False
           for task in pilot["tasks"]):
        raise ValueError("plumbing replay requires both pilot flags to remain false")
    with _blocked_external_network(network_log):
        records = [await _execute(task) for task in pilot["tasks"]]
    network_rows = [json.loads(line) for line in network_log.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
    external = sum(row.get("kind") == "external" for row in network_rows)
    payload = {
        "schema_version": "agentbench-scorer-plumbing-v1",
        "mode": "scorer_plumbing_fixture_replay", "agent_executed": False,
        "claim_boundary": "validates scorer plumbing only; not Agent task success",
        "pilot_sha256": hashlib.sha256(PILOT.read_bytes()).hexdigest(),
        "task_count": len(records), "passed_count": sum(row["passed"] for row in records),
        "external_connect_attempts": external, "paid_requests": 0 if external == 0 else None,
        "records": records,
    }
    payload["records_sha256"] = _sha(records)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if external or not all(row["passed"] for row in records):
        raise RuntimeError("scorer plumbing replay failed")
    return payload


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _tracked_clean() -> bool:
    return (subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=ROOT).returncode == 0
            and subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT).returncode == 0)


def write_manifest(output: Path, network_log: Path, manifest: Path, payload: dict) -> dict:
    runner = Path(__file__)
    value = {
        "schema_version": "agentbench-scorer-plumbing-manifest-v1",
        "source_commit": _git("rev-parse", "HEAD"),
        "source_tree": _git("rev-parse", "HEAD^{tree}"),
        "tracked_worktree_clean": _tracked_clean(),
        "runner_sha256": hashlib.sha256(runner.read_bytes()).hexdigest(),
        "pilot_sha256": payload["pilot_sha256"],
        "evidence_file_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "network_log_sha256": hashlib.sha256(network_log.read_bytes()).hexdigest(),
        "records_sha256": payload["records_sha256"],
        "task_count": payload["task_count"],
        "passed_count": payload["passed_count"],
        "agent_executed": False,
        "claim_boundary": payload["claim_boundary"],
    }
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--network-log", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    payload = asyncio.run(run(args.output, args.network_log))
    write_manifest(args.output, args.network_log, args.manifest, payload)
    print(json.dumps({key: payload[key] for key in (
        "mode", "agent_executed", "task_count", "passed_count", "external_connect_attempts")},
        ensure_ascii=False))


if __name__ == "__main__":
    main()
