"""Structural and provenance validator for registry-backed pilot drafts."""
from __future__ import annotations

import hashlib
import json
import pathlib

from scripts.eval.agentbench import inventory, pilot_oracles

ROOT = pathlib.Path(__file__).resolve().parents[3]
TOOL = {"search.catalog": "product_search_tool", "search.category_insight": "category_insight_tool",
        "memory.remember": "remember_preference_tool", "state.constraint_carry": "product_search_tool",
        "dispatch.search": "search_agent", "trade.prepare": "create_order_tool"}


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate(path: pathlib.Path) -> list[str]:
    errors = []
    data = json.loads(path.read_text(encoding="utf-8"))
    registry = json.loads((ROOT / "stage2/pilot/conversion-registry.json").read_text(encoding="utf-8"))
    entries = {entry["registry_id"]: entry for entry in registry["entries"]}
    catalog = {item["leakage_group_id"]: item for item in
               json.loads((ROOT / "eval/agent/v1/template-catalog.json").read_text(encoding="utf-8"))["templates"]}
    matrix = {item["capability_id"]: item for item in
              json.loads((ROOT / "eval/agent/v1/capability-matrix.json").read_text(encoding="utf-8"))["capabilities"]}
    records = [json.loads(line) for line in
               (ROOT / "eval/agent/v1/inventory/record-index.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    if data.get("candidate_count") != len(data.get("tasks") or []):
        errors.append("candidate_count does not match tasks")
    if len(data.get("tasks") or []) != len(entries):
        errors.append("pilot tasks do not match registry entries")
    for task in data.get("tasks") or []:
        tid = task.get("task_id", "<missing>")
        entry = entries.get(task.get("registry_id"))
        if entry is None:
            errors.append(f"{tid}: no registry entry")
            continue
        group = catalog.get(task.get("leakage_group_id"))
        if group is None or group.get("evaluation_split") != "dev":
            errors.append(f"{tid}: leakage group is not Dev")
        if task.get("source_splits") and set(task["source_splits"]) - {"dev"}:
            errors.append(f"{tid}: source split is not Dev")
        record = next((row for row in records
                       if row.get("record_id") == task.get("source_record_id")
                       and row.get("asset_path") == task.get("source_asset")
                       and row.get("leakage_group_id") == task.get("leakage_group_id")), None)
        if record is None:
            errors.append(f"{tid}: source record does not match asset/group")
        if not task.get("turns") or not isinstance(task["turns"][0].get("content"), str):
            errors.append(f"{tid}: turn content must be a string")
        if _sha(task.get("turns")) != task.get("converted_turns_sha256"):
            errors.append(f"{tid}: converted turns hash mismatch")
        spec = next((item for item in inventory.SPECS if item["path"] == task.get("source_asset")), None)
        if spec is None:
            errors.append(f"{tid}: unknown source asset")
        else:
            rows, _ = inventory.load_rows(ROOT / task["source_asset"])
            raw = next((row.get(spec["prompt_field"]) for row in rows
                        if inventory.canonical_id(row, spec) == task["source_record_id"]), None)
            if raw is None:
                errors.append(f"{tid}: source payload not found")
            else:
                if _sha(raw) != task.get("source_payload_sha256"):
                    errors.append(f"{tid}: source payload hash mismatch")
                if inventory.prompt_hashes(raw)[1] != task.get("source_prompt_sha256"):
                    errors.append(f"{tid}: source prompt hash mismatch")
        if entry.get("capability_id") != task.get("capability"):
            errors.append(f"{tid}: capability differs from registry")
        if entry.get("expected_execution_plans") != task.get("acceptable_execution_plans"):
            errors.append(f"{tid}: plans differ from registry")
        if entry.get("expected_tool_parameters") != task.get("expected_tool_parameters"):
            errors.append(f"{tid}: parameters differ from registry")
        if entry.get("oracle_id") != task.get("oracle_id") or entry.get(
                "oracle_adapter_version") != task.get("oracle_version"):
            errors.append(f"{tid}: oracle id/version differs from registry")
        if entry.get("adapter_id") != (task.get("conversion") or {}).get("adapter_id"):
            errors.append(f"{tid}: adapter differs from registry")
        if entry.get("adapter_version") != (task.get("conversion") or {}).get("adapter_version"):
            errors.append(f"{tid}: adapter version differs from registry")
        if spec is not None:
            row = next((item for item in rows if inventory.canonical_id(item, spec)
                        == task["source_record_id"]), None)
            if row is not None:
                oracle = pilot_oracles.extract(task["source_asset"], row)
                if pilot_oracles.sha(oracle) != task.get("source_oracle_sha256"):
                    errors.append(f"{tid}: source oracle hash mismatch")
                if pilot_oracles.checks(task["source_asset"], oracle) != task.get("deterministic_checks"):
                    errors.append(f"{tid}: checks are not inherited from the source oracle")
                raw = row.get(spec["prompt_field"])
                adapter = task["conversion"]["adapter_id"]
                if adapter == "identity":
                    applied = raw if isinstance(raw, str) else None
                elif adapter == "join-list":
                    applied = "；".join(raw) if isinstance(raw, list) and all(
                        isinstance(item, str) for item in raw) else None
                else:
                    applied = None
                if applied != task["turns"][0]["content"]:
                    errors.append(f"{tid}: adapter output does not match turns")
        capability = task.get("capability")
        if matrix.get(capability, {}).get("verification_status") != "verified":
            errors.append(f"{tid}: capability is not verified")
        steps = {step for plan in task.get("acceptable_execution_plans") or [] for step in plan}
        if TOOL.get(capability) not in steps:
            errors.append(f"{tid}: execution plan does not cover the capability tool")
        required_tools = (task.get("source_oracle") or {}).get("required_tools") or []
        if any(tool not in steps for tool in required_tools):
            errors.append(f"{tid}: execution plan misses a required source tool")
        for check in task.get("deterministic_checks") or []:
            if not {"kind", "field", "operator", "expected"} <= set(check):
                errors.append(f"{tid}: check lacks task-level expected")
        if task.get("automatic_scoring") is not False:
            errors.append(f"{tid}: automatic_scoring must be false until a dry-run exists")
    return errors


def main() -> None:
    import sys
    path = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "stage2/pilot/pilot-candidates.json"
    errors = validate(path)
    print(json.dumps({"passed": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
