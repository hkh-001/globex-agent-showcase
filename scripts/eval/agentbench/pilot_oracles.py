"""Versioned source-oracle extraction for pilot tasks."""
from __future__ import annotations

import hashlib
import json

ADAPTERS = {
    "eval/v1/product_retrieval.jsonl": ("product-retrieval", "v1"),
    "eval/v1/knowledge_retrieval.jsonl": ("knowledge-source", "v1"),
    "eval/v1/agent_cases.yaml": ("agent-contract", "v1"),
}


def sha(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def extract(asset: str, row: dict) -> dict:
    adapter = ADAPTERS[asset][0]
    if adapter == "product-retrieval":
        return {
            "relevant": list(row.get("relevant") or []),
            "relevant_canonical_ids": list(row.get("relevant_canonical_ids") or []),
            "target_currency": (row.get("constraints") or {}).get("target_currency"),
            "require_in_stock": bool(row.get("require_in_stock")),
            "category": row.get("category"),
        }
    if adapter == "knowledge-source":
        return {"relevant": list(row.get("relevant") or [])}
    if adapter == "agent-contract":
        return {"required_tools": list((row.get("expected") or {}).get("required_tools") or [])}
    raise ValueError(asset)


def checks(asset: str, oracle: dict) -> list[dict]:
    adapter = ADAPTERS[asset][0]
    if adapter == "product-retrieval":
        result = [
            {"kind": "event", "selector": "tool.result:product_search_tool",
             "field": "hits[].product_id", "operator": "contains_any",
             "expected": oracle["relevant"]},
            {"kind": "event", "selector": "tool.result:product_search_tool",
             "field": "hits[].canonical_product_id", "operator": "contains_any",
             "expected": oracle["relevant_canonical_ids"]},
            {"kind": "event", "selector": "tool.result:product_search_tool",
             "field": "hits[].currency", "operator": "all_equal",
             "expected": oracle["target_currency"]},
        ]
        if oracle["require_in_stock"]:
            result.append({"kind": "event", "selector": "tool.result:product_search_tool",
                           "field": "hits[].skus[].stock", "operator": "any_gt", "expected": 0})
        return result
    if adapter == "knowledge-source":
        return [
            {"kind": "tool_result", "selector": "category_insight_tool",
             "field": "insights[].source", "operator": "contains_any",
             "expected": oracle["relevant"]},
            {"kind": "event", "selector": "tool.result:category_insight_tool",
             "field": "hit_count", "operator": "scalar_gt", "expected": 0},
            {"kind": "event", "selector": "tool.result:category_insight_tool",
             "field": "policy_fact_statuses", "operator": "exists", "expected": True},
        ]
    if adapter == "agent-contract":
        return [
            {"kind": "event", "selector": "tool.result:task_dispatch", "field": "agent",
             "operator": "scalar_equals", "expected": "search_agent"},
            {"kind": "event", "selector": "tool.result:task_dispatch", "field": "status",
             "operator": "scalar_equals", "expected": "completed"},
            {"kind": "event", "selector": "tool.result:product_search_tool", "field": "hits",
             "operator": "nonempty", "expected": True},
            {"kind": "tool", "selector": "execution", "field": "invoked_tools",
             "operator": "contains_all",
             "expected": oracle["required_tools"]},
        ]
    raise ValueError(asset)
