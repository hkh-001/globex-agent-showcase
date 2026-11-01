#!/usr/bin/env python3
"""Run the zero-paid-call AgentBench Stage 0 infrastructure spike."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import jsonschema

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.eval.agentbench.scorer import score_execution_graph  # noqa: E402


SPEC = ROOT / "eval/agent/v1-spike"
TESTS = (
    "tests/test_agentbench_stage0.py",
    "tests/test_search_constraint_continuity.py",
    "tests/test_session_fencing.py",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, check=True, capture_output=True,
    ).stdout.strip()


def validate_tasks() -> list[dict]:
    schema = json.loads((SPEC / "schema.json").read_text())
    tasks = []
    for path in sorted((SPEC / "tasks").glob("*.json")):
        task = json.loads(path.read_text())
        jsonschema.Draft202012Validator(schema).validate(task)
        tasks.append({"task_id": task["task_id"], "path": str(path.relative_to(ROOT)), "sha256": digest(path)})
    if {item["task_id"] for item in tasks} != {"S-search", "T-transaction", "C-context"}:
        raise RuntimeError("Stage 0 must contain exactly the frozen S/T/C spike tasks")
    return tasks


def scorer_probes() -> dict:
    contract = json.loads((SPEC / "tasks/S-search.json").read_text())["execution_contract"]
    direct = [{"type": "tool.invoke", "payload": {"tool": "product_search_tool"},
               "correlation": {"agent": "main_agent"}}]
    dispatched = [
        {"type": "agent.dispatch", "payload": {"agent": "search_agent", "dispatch_id": "d1"},
         "correlation": {"agent": "main_agent", "dispatch_id": "d1"}},
        {"type": "tool.invoke", "payload": {"tool": "product_search_tool"},
         "correlation": {"agent": "search_agent", "dispatch_id": "d1"}},
        {"type": "tool.result", "payload": {"tool": "task_dispatch", "dispatch_id": "d1"},
         "correlation": {"agent": "main_agent", "dispatch_id": "d1"}},
    ]
    missing = json.loads(json.dumps(dispatched))
    del missing[1]["correlation"]["agent"]
    forbidden = json.loads(json.dumps(dispatched))
    forbidden[1]["payload"]["tool"] = "create_order_tool"
    rows = {
        "direct_main": score_execution_graph(direct, contract),
        "dispatched_search": score_execution_graph(dispatched, contract),
        "missing_agent": score_execution_graph(missing, contract),
        "forbidden_edge": score_execution_graph(forbidden, contract),
    }
    expected = {
        "direct_main": "passed",
        "dispatched_search": "passed",
        "missing_agent": "evidence_invalid",
        "forbidden_edge": "task_failed",
    }
    if any(rows[name]["status"] != status for name, status in expected.items()):
        raise RuntimeError("execution scorer probes did not produce the frozen outcomes")
    return rows


def run_pytest(output: Path) -> dict:
    command = [sys.executable, "-m", "pytest", "-q", *TESTS]
    environment = dict(__import__("os").environ)
    for name in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        environment.pop(name, None)
    process = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, env=environment)
    (output / "pytest.log").write_text(process.stdout + process.stderr)
    return {"command": command, "exit_code": process.returncode, "passed": process.returncode == 0}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if ROOT == output or ROOT in output.parents and ".globex-runtime" not in output.parts:
        raise SystemExit("output inside the repository must be under .globex-runtime")
    output.mkdir(parents=True, exist_ok=False)

    tasks = validate_tasks()
    probes = scorer_probes()
    tests = run_pytest(output)
    tracked_clean = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"], cwd=ROOT).returncode == 0
    cached_clean = subprocess.run(["git", "diff", "--cached", "--quiet", "HEAD", "--"], cwd=ROOT).returncode == 0
    manifest = {
        "schema_version": "agentbench-stage0-evidence-v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": git("rev-parse", "HEAD"),
        "tracked_worktree_clean": tracked_clean and cached_clean,
        "external_model_requests": 0,
        "paid_requests": 0,
        "tasks": tasks,
        "scorer_probes": probes,
        "tests": tests,
        "passed": tests["passed"] and all(value["status"] == {
            "direct_main": "passed", "dispatched_search": "passed",
            "missing_agent": "evidence_invalid", "forbidden_edge": "task_failed",
        }[name] for name, value in probes.items()),
        "scope": "evaluation infrastructure only; no model-capability claim",
    }
    write_json(output / "manifest.json", manifest)
    hashes = {path.name: digest(path) for path in sorted(output.iterdir()) if path.is_file()}
    write_json(output / "files.sha256.json", hashes)
    print(json.dumps({"output": str(output), "passed": manifest["passed"], "paid_requests": 0}, ensure_ascii=False))
    return 0 if manifest["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
