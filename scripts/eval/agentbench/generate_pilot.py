"""Generate a small deterministic Dev pilot from verified capabilities."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
MODULES = {
    "search": ["search.catalog", "search.constraints", "search.exact_id", "search.landed_price"],
    "knowledge": ["search.category_insight"],
    "memory": ["memory.remember", "memory.update", "memory.forget"],
    "context": ["state.constraint_carry", "state.session_isolation"],
    "routing": ["dispatch.search", "dispatch.trade"],
    "transaction": ["trade.prepare", "trade.query", "trade.approve", "trade.cancel", "trade.idempotency"],
}
CHECKS = {
    "search": [{"kind": "event", "field": "hit_count", "operator": "gt", "expected": 0},
               {"kind": "event", "field": "hits", "operator": "nonempty", "expected": True}],
    "knowledge": [{"kind": "event", "field": "hit_count", "operator": "gt", "expected": 0},
                  {"kind": "event", "field": "abstained", "operator": "equals", "expected": False}],
    "memory": [{"kind": "event", "field": "saved", "operator": "exists", "expected": True}],
    "context": [{"kind": "event", "field": "correlation.session_id", "operator": "exists", "expected": True}],
    "routing": [{"kind": "event", "field": "status", "operator": "equals", "expected": "completed"}],
    "transaction": [{"kind": "event", "field": "confirmation.status", "operator": "equals", "expected": "pending"}],
}
PARAMS = {
    "search": {"normalized_query": "旅行背包", "ship_to": "CN", "top_k": 5},
    "knowledge": {"question": "美国免税额度是多少", "top_k": 2},
    "memory": {"kind": "like", "statement": "喜欢轻便背包"},
    "context": {"max_turns": 2},
    "routing": {"agent": "search_agent", "demands": "find a backpack"},
    "transaction": {"product_id": "P1001", "quantity": 1},
}
PLANS = {
    "search": [["main_agent", "product_search_tool"], ["main_agent", "search_agent", "product_search_tool"]],
    "knowledge": [["main_agent", "category_insight_tool"], ["main_agent", "search_agent", "category_insight_tool"]],
    "memory": [["main_agent", "remember_preference_tool"]],
    "context": [["main_agent"], ["main_agent", "search_agent", "product_search_tool"]],
    "routing": [["main_agent", "search_agent"], ["main_agent", "trade_agent"]],
    "transaction": [["main_agent", "trade_agent", "create_order_tool"], ["main_agent", "create_order_tool"]],
}


def build() -> dict:
    catalog = json.loads((ROOT / "eval/agent/v1/template-catalog.json").read_text(encoding="utf-8"))
    matrix = json.loads((ROOT / "eval/agent/v1/capability-matrix.json").read_text(encoding="utf-8"))
    verified = {item["capability_id"] for item in matrix["capabilities"]
                if item.get("verification_status") == "verified"}
    tasks = []
    for module, capabilities in MODULES.items():
        group = next((item for item in sorted(catalog["templates"], key=lambda row: row["leakage_group_id"])
                      if item["module"] == module and item["recommended_split"] == "dev"), None)
        if group is None:
            continue
        chosen = [cap for cap in capabilities if cap in verified][:2]
        if not chosen:
            continue
        digest = hashlib.sha256(group["leakage_group_id"].encode()).hexdigest()[:8]
        tasks.append({
            "task_id": f"pilot-{module}-{digest}",
            "module": module,
            "source_asset": group["source_assets"][0] if group["source_assets"] else None,
            "leakage_group_id": group["leakage_group_id"],
            "split": "dev",
            "acceptable_execution_plans": PLANS[module],
            "required_capabilities": chosen,
            "forbidden_capabilities": ["search.web"],
            "allowed_agents": sorted({agent for plan in PLANS[module] for agent in plan if agent.endswith("_agent")}),
            "allowed_edges": [[left, right] for plan in PLANS[module]
                              for left, right in zip(plan, plan[1:])],
            "forbidden_edges": [["main_agent", "web_search_tool"]],
            "expected_tool_parameters": PARAMS[module],
            "deterministic_checks": CHECKS[module],
            "oracle_type": "deterministic",
            "side_effect_policy": {"before_after_snapshot": "trade_repository" if module == "transaction" else "none",
                                   "allow_order_create": module == "transaction"},
            "isolation": {"buyer_id": f"pilot-{module}-buyer", "session_id": f"pilot-{module}-session",
                          "database": "fresh_per_task"},
            "max_turns": 6,
            "max_model_calls": 3,
            "human_review": {"required": False, "scope": "none"},
            "automatic_scoring": True,
            "data_limits": "dev-only; holdout locked; one leakage group per task",
            "power_note": "pilot only; not powered for a module-level success claim",
        })
    return {
        "schema_version": "agentbench-pilot-candidates-v1",
        "candidate_count": len(tasks),
        "formal_eval_size": False,
        "holdout_used": False,
        "note": "pilot candidates, not the formal 60-task suite; one leakage group per task",
        "tasks": tasks,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=pathlib.Path, default=ROOT / "eval/agent/v2/pilot-candidates.json")
    args = parser.parse_args()
    payload = build()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"candidate_count": payload["candidate_count"],
                      "modules": [task["module"] for task in payload["tasks"]]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
