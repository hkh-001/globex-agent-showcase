"""Structural validator for pilot candidates (no model calls)."""
from __future__ import annotations

import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]


def validate(path: pathlib.Path) -> list[str]:
    errors = []
    data = json.loads(path.read_text(encoding="utf-8"))
    catalog = {item["leakage_group_id"]: item for item in
               json.loads((ROOT / "eval/agent/v1/template-catalog.json").read_text(encoding="utf-8"))["templates"]}
    matrix = {item["capability_id"]: item for item in
              json.loads((ROOT / "eval/agent/v1/capability-matrix.json").read_text(encoding="utf-8"))["capabilities"]}
    records = [json.loads(line) for line in
               (ROOT / "eval/agent/v1/inventory/record-index.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    if data.get("candidate_count") != len(data.get("tasks") or []):
        errors.append("candidate_count does not match tasks")
    for task in data.get("tasks") or []:
        tid = task.get("task_id", "<missing>")
        group = catalog.get(task.get("leakage_group_id"))
        if group is None or group.get("recommended_split") != "dev":
            errors.append(f"{tid}: leakage group is not Dev")
        if not task.get("turns"):
            errors.append(f"{tid}: no user turns")
        record = next((row for row in records
                       if row.get("record_id") == task.get("source_record_id")
                       and row.get("leakage_group_id") == task.get("leakage_group_id")), None)
        if record is None:
            errors.append(f"{tid}: source record does not belong to the group")
        capability = task.get("capability")
        entry = matrix.get(capability)
        if entry is None or entry.get("verification_status") != "verified":
            errors.append(f"{tid}: capability is not verified")
        else:
            declared = {(obs["source_type"], obs["selector"], tuple(obs["fields"]))
                        for obs in entry["observations"]}
            for check in task.get("deterministic_checks") or []:
                key = (check.get("source_type"), check.get("selector"), tuple(check.get("fields") or []))
                if key not in declared:
                    errors.append(f"{tid}: check does not match a declared observation")
        plans = task.get("acceptable_execution_plans") or []
        if not plans or any(not plan for plan in plans):
            errors.append(f"{tid}: empty execution plan")
        if task.get("automatic_scoring") is not False:
            errors.append(f"{tid}: automatic_scoring must be false until a dry-run exists")
    return errors


def main() -> None:
    import sys
    path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "eval/agent/v2/pilot-candidates.json"
    errors = validate(path)
    print(json.dumps({"passed": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
