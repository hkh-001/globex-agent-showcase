"""Generate pilot drafts strictly from the conversion registry."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib

from scripts.eval.agentbench import inventory, pilot_oracles

ROOT = pathlib.Path(__file__).resolve().parents[3]
REGISTRY = ROOT / "stage2/pilot/conversion-registry.json"
TOOL = {"search.catalog": "product_search_tool", "search.category_insight": "category_insight_tool",
        "memory.remember": "remember_preference_tool", "state.constraint_carry": "product_search_tool",
        "dispatch.search": "search_agent", "trade.prepare": "create_order_tool"}


def _sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _resolve(entry: dict):
    path = entry["source_asset"]
    spec = next((item for item in inventory.SPECS if item["path"] == path), None)
    if spec is None:
        raise SystemExit(f"{entry['registry_id']}: unknown source asset")
    rows, _ = inventory.load_rows(ROOT / path)
    for row in rows:
        if inventory.canonical_id(row, spec) != entry["source_record_id"]:
            continue
        raw = row.get(spec["prompt_field"])
        adapter = entry["adapter_id"]
        if adapter == "identity":
            if not isinstance(raw, str):
                raise SystemExit(f"{entry['registry_id']}: identity adapter needs a string")
            content = raw
        elif adapter == "join-list":
            if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
                raise SystemExit(f"{entry['registry_id']}: join-list adapter needs a list of strings")
            content = "；".join(raw)
        else:
            raise SystemExit(f"{entry['registry_id']}: unknown adapter")
        _, normalized, _ = inventory.prompt_hashes(raw)
        if normalized != entry["source_prompt_sha256"]:
            raise SystemExit(f"{entry['registry_id']}: source prompt hash mismatch")
        return raw, normalized, content, row
    raise SystemExit(f"{entry['registry_id']}: record not found")


def build() -> dict:
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    catalog = {item["leakage_group_id"]: item for item in
               json.loads((ROOT / "eval/agent/v1/template-catalog.json").read_text(encoding="utf-8"))["templates"]}
    matrix = {item["capability_id"]: item for item in
              json.loads((ROOT / "eval/agent/v1/capability-matrix.json").read_text(encoding="utf-8"))["capabilities"]}
    tasks = []
    for entry in registry["entries"]:
        group = catalog[entry["source_leakage_group_id"]]
        if group["evaluation_split"] != "dev":
            raise SystemExit(f"{entry['registry_id']}: evaluation split is not Dev")
        entry_capability = matrix[entry["capability_id"]]
        if entry_capability.get("verification_status") != "verified":
            raise SystemExit(f"{entry['registry_id']}: capability is not verified")
        raw, normalized, content, row = _resolve(entry)
        oracle = pilot_oracles.extract(entry["source_asset"], row)
        if oracle != entry.get("source_oracle") or pilot_oracles.sha(oracle) != entry.get(
                "source_oracle_sha256"):
            raise SystemExit(f"{entry['registry_id']}: source oracle mismatch")
        turns = [{"role": "user", "content": content}]
        tasks.append({
            "task_id": f"pilot-{entry['registry_id']}",
            "registry_id": entry["registry_id"],
            "module": group["module"],
            "capability": entry["capability_id"],
            "source_asset": entry["source_asset"],
            "source_record_id": entry["source_record_id"],
            "source_prompt_sha256": normalized,
            "source_payload_sha256": _sha(raw),
            "source_splits": group["source_splits"],
            "converted_turns_sha256": _sha(turns),
            "leakage_group_id": group["leakage_group_id"],
            "split": group["evaluation_split"],
            "conversion": {"adapter_id": entry["adapter_id"], "adapter_version": entry["adapter_version"],
                           "reason": entry["conversion_reason"], "registry_id": entry["registry_id"]},
            "turns": turns,
            "acceptable_execution_plans": entry["expected_execution_plans"],
            "required_capabilities": [entry["capability_id"]],
            "forbidden_capabilities": entry["task_oracle"]["forbidden_capabilities"],
            "allowed_agents": sorted({agent for plan in entry["expected_execution_plans"]
                                      for agent in plan if agent.endswith("_agent")}),
            "allowed_edges": [[left, right] for plan in entry["expected_execution_plans"]
                              for left, right in zip(plan, plan[1:])],
            "forbidden_edges": [["main_agent", "web_search_tool"]],
            "expected_tool_parameters": entry["expected_tool_parameters"],
            "deterministic_checks": pilot_oracles.checks(entry["source_asset"], oracle),
            "source_oracle": oracle,
            "source_oracle_sha256": entry["source_oracle_sha256"],
            "oracle_adapter_id": entry["oracle_adapter_id"],
            "oracle_adapter_version": entry["oracle_adapter_version"],
            "oracle_id": entry["oracle_id"],
            "oracle_version": entry["oracle_adapter_version"],
            "side_effect_policy": {"before_after_snapshot": "none", "expected": entry["task_oracle"]["side_effects"]},
            "isolation": {"buyer_id": f"pilot-{entry['registry_id']}-buyer",
                          "session_id": f"pilot-{entry['registry_id']}-session",
                          "database": "fresh_per_task"},
            "max_turns": 2,
            "max_model_calls": 2,
            "human_review": {"required": False, "scope": "none"},
            "automatic_scoring": False,
            "execution_ready": False,
            "blocked_reason": "dry-run scorer not implemented; draft only",
            "data_limitations": entry["task_oracle"]["data_limitations"],
            "power_note": "pilot only; not powered for a module-level success claim",
        })
    return {
        "schema_version": "agentbench-pilot-candidates-v3",
        "candidate_count": len(tasks),
        "formal_eval_size": False,
        "holdout_used": False,
        "executable": False,
        "note": "registry-backed pilot drafts; not the formal 60-task suite",
        "tasks": tasks,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=pathlib.Path, default=ROOT / "stage2/pilot/pilot-candidates.json")
    args = parser.parse_args()
    payload = build()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"candidate_count": payload["candidate_count"],
                      "tasks": [task["task_id"] for task in payload["tasks"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
