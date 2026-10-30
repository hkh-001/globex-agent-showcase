"""Run the public, deterministic Globex business contract baseline.

This is an engineering regression suite, not an LLM quality evaluation.  Each
case is an existing public pytest contract and runs without model credentials.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "eval/business/public-cases-v1.json"
PROXY_KEYS = ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")


def load_cases(path: Path) -> dict:
    suite = json.loads(path.read_text(encoding="utf-8"))
    cases = suite.get("cases")
    if suite.get("schema_version") != "globex.public-business.v1" or not isinstance(cases, list):
        raise ValueError("unsupported public business suite")
    ids = [case.get("id") for case in cases]
    nodeids = [case.get("nodeid") for case in cases]
    if not 10 <= len(cases) <= 20 or len(ids) != len(set(ids)) or len(nodeids) != len(set(nodeids)):
        raise ValueError("suite must contain 10-20 unique cases and nodeids")
    for case in cases:
        if not all(isinstance(case.get(key), str) and case[key] for key in ("id", "domain", "title", "nodeid")):
            raise ValueError("case identity fields are required")
        if not isinstance(case.get("metrics"), list) or not case["metrics"]:
            raise ValueError("every case must map to at least one metric")
        source = ROOT / case["nodeid"].split("::", 1)[0]
        if not source.is_file() or not str(source.resolve()).startswith(str((ROOT / "tests").resolve())):
            raise ValueError(f"case {case['id']} must reference a public tests/ node")
    return suite


def rate(passed: int, total: int) -> dict:
    return {"passed": passed, "total": total, "rate": passed / total if total else None}


def summarize(rows: list[dict]) -> dict:
    domains: dict[str, list[bool]] = defaultdict(list)
    metrics: dict[str, list[bool]] = defaultdict(list)
    for row in rows:
        domains[row["domain"]].append(row["passed"])
        for metric in row["metrics"]:
            metrics[metric].append(row["passed"])
    passed = sum(row["passed"] for row in rows)
    return {
        "case_pass_rate": rate(passed, len(rows)),
        "by_domain": {name: rate(sum(values), len(values)) for name, values in sorted(domains.items())},
        "contract_metrics": {name: rate(sum(values), len(values)) for name, values in sorted(metrics.items())},
        "external_chat_model_access": "disabled",
        "physical_model_call_count": "not instrumented",
        "metric_scope": "deterministic public engineering contracts; not model/agent answer quality",
    }


def junit_execution(path: Path) -> dict:
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    if not path.is_file():
        return {**counts, "executed_passes": 0, "valid_single_pass": False}
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    # testsuites parents repeat child totals; only leaf suites own testcase records.
    leaves = [suite for suite in suites if list(suite.iter("testcase"))]
    cases = [case for suite in leaves for case in suite.findall("testcase")]
    counts["tests"] = len(cases)
    counts["failures"] = sum(case.find("failure") is not None for case in cases)
    counts["errors"] = sum(case.find("error") is not None for case in cases)
    counts["skipped"] = sum(case.find("skipped") is not None for case in cases)
    executed_passes = counts["tests"] - counts["failures"] - counts["errors"] - counts["skipped"]
    return {**counts, "executed_passes": executed_passes,
            "valid_single_pass": counts == {"tests": 1, "failures": 0, "errors": 0, "skipped": 0}}


def git_source_state(root: Path = ROOT) -> dict:
    """Sample source state without exposing untracked path names."""
    def git(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)

    head = git("rev-parse", "HEAD")
    unstaged = git("diff", "--quiet", "HEAD", "--")
    staged = git("diff", "--cached", "--quiet")
    status = git("status", "--porcelain", "--untracked-files=normal")
    lines = status.stdout.splitlines() if status.returncode == 0 else []
    return {
        "source_commit": head.stdout.strip() if head.returncode == 0 else None,
        "tracked_worktree_clean": unstaged.returncode == 0 and staged.returncode == 0,
        "working_tree_clean": status.returncode == 0 and not lines,
        "untracked_count": sum(line.startswith("?? ") for line in lines),
        "sampled_before_output_creation": True,
    }


def source_is_stable(start: dict, finish: dict) -> bool:
    """Require the same commit and clean tracked state at both sampled endpoints."""
    return bool(start.get("source_commit")) and start.get("source_commit") == finish.get("source_commit") \
        and start.get("tracked_worktree_clean") is True and finish.get("tracked_worktree_clean") is True


def run(suite_path: Path, output: Path) -> dict:
    suite = load_cases(suite_path)
    source_at_start = git_source_state()
    env = os.environ.copy()
    cleared = [key for key in PROXY_KEYS if env.pop(key, None) is not None]
    pytest_addopts_cleared = env.pop("PYTEST_ADDOPTS", None) is not None
    env["LLM_API_KEY"] = "test-only"
    env["LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
    env["TAVILY_API_KEY"] = ""
    rows = []
    with tempfile.TemporaryDirectory(prefix="globex-public-business-") as temporary:
        temporary_path = Path(temporary)
        for case in suite["cases"]:
            started = time.monotonic()
            junit = temporary_path / f"{case['id']}.xml"
            command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                       "-o", "addopts=", f"--junitxml={junit}", case["nodeid"]]
            try:
                completed = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True, timeout=120)
                execution = junit_execution(junit)
                passed, exit_code = completed.returncode == 0 and execution["valid_single_pass"], completed.returncode
                output_tail = (completed.stdout + completed.stderr)[-2000:]
            except subprocess.TimeoutExpired as error:
                execution = junit_execution(junit)
                passed, exit_code = False, None
                output_tail = f"TIMEOUT after {error.timeout} seconds"
            rows.append({**case, "passed": passed, "exit_code": exit_code, "execution": execution,
                         "elapsed_ms": round((time.monotonic() - started) * 1000, 3), "output_tail": output_tail})
    dataset_bytes = suite_path.read_bytes()
    source_at_finish = git_source_state()
    report = {
        "schema_version": "globex.public-business-report.v2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "suite": str(suite_path.relative_to(ROOT)),
        "suite_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
        "source_commit": source_at_start["source_commit"],
        "source_state_at_start": source_at_start,
        # Endpoint sampling cannot prove the tree never changed briefly during execution.
        "source_stable": source_is_stable(source_at_start, source_at_finish),
        "environment": {"proxy_variables_cleared": cleared, "pytest_addopts_cleared": pytest_addopts_cleared,
                        "external_chat_model_base_url": "local_unreachable", "chat_model_credentials_used": False},
        "summary": summarize(rows),
        "cases": rows,
        "result": "pass" if all(row["passed"] for row in rows) else "fail",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.cases.resolve(), args.output.resolve())
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["result"] == "pass" else 1)


if __name__ == "__main__":
    main()
