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
            {"kind": "value", "field": "hits[].product_id", "operator": "in",
             "expected": oracle["relevant"]},
            {"kind": "value", "field": "hits[].canonical_product_id", "operator": "in",
             "expected": oracle["relevant_canonical_ids"]},
            {"kind": "value", "field": "hits[].landed_price.currency", "operator": "equals",
             "expected": oracle["target_currency"]},
        ]
        if oracle["require_in_stock"]:
            result.append({"kind": "value", "field": "hits[].skus[].stock", "operator": "gt",
                           "expected": 0})
        return result
    if adapter == "knowledge-source":
        return [
            {"kind": "value", "field": "insights[].source", "operator": "in",
             "expected": oracle["relevant"]},
            {"kind": "value", "field": "hit_count", "operator": "gt", "expected": 0},
            {"kind": "value", "field": "policy_fact_statuses", "operator": "exists", "expected": True},
        ]
    if adapter == "agent-contract":
        return [
            {"kind": "event", "selector": "tool.result:task_dispatch", "field": "agent",
             "operator": "equals", "expected": "search_agent"},
            {"kind": "event", "selector": "tool.result:task_dispatch", "field": "status",
             "operator": "equals", "expected": "completed"},
            {"kind": "event", "selector": "tool.result:product_search_tool", "field": "hits",
             "operator": "nonempty", "expected": True},
            {"kind": "tool", "field": "required_tools", "operator": "included",
             "expected": oracle["required_tools"]},
        ]
    raise ValueError(asset)
