"""Independently verify committed scorer-plumbing evidence and provenance."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from scripts.eval.agentbench import deterministic_checks, scorer
from scripts.eval.agentbench.run_pilot_plumbing import ROOT, _sha


EVIDENCE_SCHEMA = "agentbench-scorer-plumbing-v1"
MANIFEST_SCHEMA = "agentbench-scorer-plumbing-manifest-v1"
MODE = "scorer_plumbing_fixture_replay"
CLAIM_BOUNDARY = "validates scorer plumbing only; not Agent task success"


def _git(*args: str, binary: bool = False):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=not binary)


def check(evidence_path: Path, network_path: Path, manifest_path: Path) -> list[str]:
    errors: list[str] = []
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    commit = manifest.get("source_commit", "")
    try:
        tree = _git("rev-parse", f"{commit}^{{tree}}").strip()
        subprocess.run(["git", "merge-base", "--is-ancestor", commit, "HEAD"], cwd=ROOT, check=True)
    except (subprocess.CalledProcessError, ValueError):
        errors.append("source_commit_invalid")
        tree = ""
    if tree != manifest.get("source_tree"):
        errors.append("source_tree_mismatch")
    if manifest.get("tracked_worktree_clean") is not True:
        errors.append("tracked_worktree_not_clean")
    for key, value in {
        "evidence_file_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
        "network_log_sha256": hashlib.sha256(network_path.read_bytes()).hexdigest(),
    }.items():
        if manifest.get(key) != value:
            errors.append(f"{key}_mismatch")
    try:
        runner_blob = _git("show", f"{commit}:scripts/eval/agentbench/run_pilot_plumbing.py", binary=True)
        pilot_blob = _git("show", f"{commit}:stage2/pilot/pilot-candidates.json", binary=True)
    except subprocess.CalledProcessError:
        errors.append("source_blob_missing")
        runner_blob = pilot_blob = b""
    if hashlib.sha256(runner_blob).hexdigest() != manifest.get("runner_sha256"):
        errors.append("runner_sha256_mismatch")
    pilot_hash = hashlib.sha256(pilot_blob).hexdigest()
    if pilot_hash != manifest.get("pilot_sha256") or pilot_hash != evidence.get("pilot_sha256"):
        errors.append("pilot_sha256_mismatch")
    try:
        pilot = json.loads(pilot_blob)
        tasks = {task["task_id"]: task for task in pilot["tasks"]}
    except (json.JSONDecodeError, KeyError, TypeError):
        errors.append("pilot_blob_invalid")
        tasks = {}
    records = evidence.get("records") or []
    if _sha(records) != evidence.get("records_sha256") or _sha(records) != manifest.get("records_sha256"):
        errors.append("records_sha256_mismatch")
    if evidence.get("agent_executed") is not False or manifest.get("agent_executed") is not False:
        errors.append("agent_execution_claim_invalid")
    if evidence.get("schema_version") != EVIDENCE_SCHEMA:
        errors.append("evidence_schema_invalid")
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        errors.append("manifest_schema_invalid")
    if evidence.get("mode") != MODE:
        errors.append("evidence_mode_invalid")
    if (evidence.get("claim_boundary") != CLAIM_BOUNDARY
            or manifest.get("claim_boundary") != CLAIM_BOUNDARY):
        errors.append("claim_boundary_invalid")
    if evidence.get("external_connect_attempts") != 0 or evidence.get("paid_requests") != 0:
        errors.append("network_or_paid_requests_nonzero")
    network_rows = []
    for line in network_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            network_rows.append(json.loads(line))
        except json.JSONDecodeError:
            errors.append("network_log_invalid_json")
    external_count = sum(row.get("kind") == "external" for row in network_rows
                         if isinstance(row, dict))
    if external_count != evidence.get("external_connect_attempts") or external_count:
        errors.append("external_connect_count_mismatch")
    passed_count = sum(record.get("passed") is True for record in records)
    if (evidence.get("task_count") != len(records)
            or evidence.get("passed_count") != passed_count
            or manifest.get("task_count") != len(records)
            or manifest.get("passed_count") != passed_count):
        errors.append("task_counts_invalid")
    record_task_ids = [record.get("task_id") for record in records]
    pilot_task_ids = list(tasks)
    if len(record_task_ids) != len(set(record_task_ids)):
        errors.append("duplicate_task_id")
    if set(record_task_ids) != set(pilot_task_ids):
        errors.append("pilot_task_set_mismatch")
    if record_task_ids != pilot_task_ids:
        errors.append("pilot_task_order_mismatch")
    scopes = set()
    for index, record in enumerate(records):
        scope = record.get("scope") or {}
        scope_key = (scope.get("shopping_session_id"), scope.get("run_id"))
        if not all(scope_key) or scope_key in scopes:
            errors.append(f"record[{index}]:scope_invalid")
        scopes.add(scope_key)
        if (record.get("agent_executed") is not False or record.get("physical_model_calls") != 0
                or record.get("passed") is not True
                or not (record.get("execution_score") or {}).get("passed")
                or not (record.get("deterministic_score") or {}).get("passed")):
            errors.append(f"record[{index}]:result_invalid")
        task = tasks.get(record.get("task_id"))
        if task is None:
            errors.append(f"record[{index}]:task_missing_from_pilot")
        else:
            recomputed_execution = scorer.score_execution_graph(
                record.get("events") or [], task, scope=scope)
            recomputed_deterministic = deterministic_checks.score(
                task["deterministic_checks"], record.get("events") or [],
                tool_results=record.get("tool_results") or [], scope=scope)
            if recomputed_execution != record.get("execution_score"):
                errors.append(f"record[{index}]:execution_score_mismatch")
            if recomputed_deterministic != record.get("deterministic_score"):
                errors.append(f"record[{index}]:deterministic_score_mismatch")
            if not recomputed_execution.get("passed") or not recomputed_deterministic.get("passed"):
                errors.append(f"record[{index}]:recomputed_score_failed")
        if _sha(record.get("events") or []) != record.get("events_sha256"):
            errors.append(f"record[{index}]:events_sha256_mismatch")
        if _sha(record.get("tool_results") or []) != record.get("tool_results_sha256"):
            errors.append(f"record[{index}]:tool_results_sha256_mismatch")
        for item in [*(record.get("events") or []), *(record.get("tool_results") or [])]:
            run_id = item.get("run_id") or (item.get("correlation") or {}).get("run_id")
            if item.get("shopping_session_id") != scope_key[0] or run_id != scope_key[1]:
                errors.append(f"record[{index}]:out_of_scope_evidence")
                break
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--network-log", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    errors = check(args.evidence, args.network_log, args.manifest)
    print(json.dumps({"passed": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
