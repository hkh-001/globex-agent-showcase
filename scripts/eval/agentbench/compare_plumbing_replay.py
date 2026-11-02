"""Semantic comparison for scorer-plumbing replays across dynamic timestamps."""
from __future__ import annotations

import argparse
import json
import pathlib

DYNAMIC_KEYS = {
    "occurred_at", "started_at", "finished_at", "elapsed_ms", "created_at",
    "resolved_at", "expires_at", "timestamp", "duration_ms", "dispatch_id", "observed_at",
}


def strip_dynamic(value):
    if isinstance(value, dict):
        return {key: strip_dynamic(item) for key, item in sorted(value.items())
                if key not in DYNAMIC_KEYS}
    if isinstance(value, list):
        return [strip_dynamic(item) for item in value]
    return value


def normalize(evidence: dict) -> dict:
    records = []
    for record in evidence.get("records") or []:
        records.append({
            "task_id": record.get("task_id"),
            "scope": record.get("scope"),
            "events": strip_dynamic(record.get("events") or []),
            "tool_results": strip_dynamic(record.get("tool_results") or []),
            "execution_score": strip_dynamic(record.get("execution_score") or {}),
            "deterministic_score": strip_dynamic(record.get("deterministic_score") or {}),
            "passed": record.get("passed"),
            "agent_executed": record.get("agent_executed"),
        })
    return {"task_count": evidence.get("task_count"), "records": records}


def compare(baseline: dict, candidate: dict) -> list[str]:
    left, right = normalize(baseline), normalize(candidate)
    errors = []
    if left["task_count"] != right["task_count"]:
        errors.append("task_count")
    if [row["task_id"] for row in left["records"]] != [row["task_id"] for row in right["records"]]:
        errors.append("task_order")
    for index, (a, b) in enumerate(zip(left["records"], right["records"])):
        if a != b:
            for key in a:
                if a[key] != b.get(key):
                    errors.append(f"record[{index}].{key}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=pathlib.Path, required=True)
    parser.add_argument("--candidate", type=pathlib.Path, required=True)
    args = parser.parse_args()
    baseline = json.loads((args.baseline / "evidence.json").read_text(encoding="utf-8")) \
        if args.baseline.is_dir() else json.loads(args.baseline.read_text(encoding="utf-8"))
    candidate = json.loads((args.candidate / "evidence.json").read_text(encoding="utf-8")) \
        if args.candidate.is_dir() else json.loads(args.candidate.read_text(encoding="utf-8"))
    errors = compare(baseline, candidate)
    print(json.dumps({"passed": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
