"""Generate a small, source-backed, single-capability Dev pilot."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib

from scripts.eval.agentbench import inventory

ROOT = pathlib.Path(__file__).resolve().parents[3]
MODULE_CAPABILITY = {
    "search": "search.catalog",
    "knowledge": "search.category_insight",
    "memory": "memory.remember",
    "context": "state.constraint_carry",
    "routing": "dispatch.search",
    "transaction": "trade.prepare",
}
PLANS = {
    "search.catalog": [["main_agent", "search_agent", "product_search_tool"],
                       ["main_agent", "product_search_tool"]],
    "search.category_insight": [["main_agent", "search_agent", "category_insight_tool"],
                                ["main_agent", "category_insight_tool"]],
    "memory.remember": [["main_agent", "remember_preference_tool"]],
    "state.constraint_carry": [["main_agent", "product_search_tool"]],
    "dispatch.search": [["main_agent", "search_agent"]],
    "trade.prepare": [["main_agent", "trade_agent", "create_order_tool"],
                      ["main_agent", "create_order_tool"]],
}
TOOL = {"search.catalog": "product_search_tool", "search.category_insight": "category_insight_tool",
        "memory.remember": "remember_preference_tool", "state.constraint_carry": "product_search_tool",
        "dispatch.search": "search_agent", "trade.prepare": "create_order_tool"}


def _catalog() -> dict:
    return json.loads((ROOT / "eval/agent/v1/template-catalog.json").read_text(encoding="utf-8"))


def _records() -> list[dict]:
    return [json.loads(line) for line in
            (ROOT / "eval/agent/v1/inventory/record-index.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]


def _source_prompt(record: dict):
    spec = next((item for item in inventory.SPECS if item["path"] == record["asset_path"]), None)
    if spec is None or not spec.get("prompt_field"):
        return None
    rows, _ = inventory.load_rows(ROOT / record["asset_path"])
    for row, group in zip(rows, inventory.prompt_groups(rows, spec)):
        if group["leakage_group_id"] == record["leakage_group_id"]:
            return row.get(spec["prompt_field"])
    return None


def build() -> dict:
    catalog = _catalog()
    matrix = json.loads((ROOT / "eval/agent/v1/capability-matrix.json").read_text(encoding="utf-8"))
    by_cap = {item["capability_id"]: item for item in matrix["capabilities"]}
    records = _records()
    tasks = []
    for module, capability in MODULE_CAPABILITY.items():
        entry = by_cap[capability]
        families = set(entry.get("candidate_asset_families") or [])
        group = next((item for item in sorted(catalog["templates"], key=lambda row: row["leakage_group_id"])
                      if item["module"] == module and item["recommended_split"] == "dev"
                      and set(item["families"]) & families), None)
        if group is None:
            continue
        record = next((row for row in records if row["leakage_group_id"] == group["leakage_group_id"]), None)
        prompt = _source_prompt(record) if record else None
        if record is None or prompt is None:
            continue
        checks = [{"source_type": obs["source_type"], "selector": obs["selector"],
                   "fields": obs["fields"]} for obs in entry["observations"]]
        turns = [{"role": "user", "content": prompt}]
        if capability == "state.constraint_carry":
            turns.append({"role": "user", "content": "那就按这些条件再帮我找一下"})
        tasks.append({
            "task_id": f"pilot-{module}-{hashlib.sha256(group['leakage_group_id'].encode()).hexdigest()[:8]}",
            "module": module,
            "capability": capability,
            "source_asset": record["asset_path"],
            "source_record_id": record["record_id"],
            "source_prompt_sha256": record["normalized_prompt_sha256"],
            "leakage_group_id": group["leakage_group_id"],
            "split": "dev",
            "conversion": {"method": "prompt-hash-match", "asset": record["asset_path"],
                           "group": group["leakage_group_id"], "generator": "generate_pilot-v2"},
            "turns": turns,
            "acceptable_execution_plans": PLANS[capability],
            "required_capabilities": [capability],
            "forbidden_capabilities": ["search.web"],
            "allowed_agents": sorted({agent for plan in PLANS[capability] for agent in plan if agent.endswith("_agent")}),
            "allowed_edges": [[left, right] for plan in PLANS[capability]
                              for left, right in zip(plan, plan[1:])],
            "forbidden_edges": [["main_agent", "web_search_tool"]],
            "expected_tool_parameters": {"tool": TOOL[capability]},
            "deterministic_checks": checks,
            "oracle_type": "deterministic",
            "side_effect_policy": {"before_after_snapshot": "none"},
            "isolation": {"buyer_id": f"pilot-{module}-buyer", "session_id": f"pilot-{module}-session",
                          "database": "fresh_per_task"},
            "max_turns": len(turns) + 1,
            "max_model_calls": len(turns) + 1,
            "human_review": {"required": False, "scope": "none"},
            "automatic_scoring": False,
            "execution_ready": False,
            "blocked_reason": "dry-run scorer not implemented; pilot is not executable yet",
            "data_limits": "dev-only; holdout locked; one leakage group per task",
            "power_note": "pilot only; not powered for a module-level success claim",
        })
    return {
        "schema_version": "agentbench-pilot-candidates-v2",
        "candidate_count": len(tasks),
        "formal_eval_size": False,
        "holdout_used": False,
        "executable": False,
        "note": "source-backed pilot drafts; not the formal 60-task suite",
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
