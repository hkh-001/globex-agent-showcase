# -*- coding: utf-8 -*-
"""单轮检索硬约束连续性。

模型可以改写召回词，但不能在同一轮后续调用中静默丢掉已经建立的硬约束。
状态由 orchestrator 每轮创建；ContextVar 会把同一实例传播给 Main/Search Agent 子任务，
锁保证并行工具调用按完整约束快照串行归并。下一轮和会话恢复都会创建新实例。
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from app.domain.catalog.product_attributes import infer_product_type, mentioned_product_types, normalize_product_type


class SearchConstraintConflict(ValueError):
    pass


_OPTIONAL_PREFERENCE_TERMS = {
    "登机": ("登机", "机舱", "随身行李"),
    "轻便": ("轻便", "轻量", "超轻"),
    "大容量": ("大容量", "容量大"),
}


def _remove_unstated_preferences(raw_query: str, normalized_query: str) -> tuple[str, list[str]]:
    cleaned, removed = normalized_query, []
    for concept, terms in _OPTIONAL_PREFERENCE_TERMS.items():
        if any(term in raw_query for term in terms):
            continue
        matched = [term for term in terms if term in cleaned]
        if matched:
            for term in matched:
                cleaned = cleaned.replace(term, " ")
            removed.append(concept)
    return " ".join(cleaned.split()), removed


@dataclass
class SearchConstraintGuard:
    raw_query: str
    _constraints: dict[str, Any] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    _last_fingerprint: str | None = None
    _last_result: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self._turn_types = mentioned_product_types(self.raw_query)

    async def reconcile(self, supplied: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        """返回带继承约束的参数；精确 ID 查询不继承普通召回类型。"""
        args = dict(supplied)
        exact_id = bool(args.get("product_id") or args.get("sku_id"))
        cleaned_query, removed_query_terms = _remove_unstated_preferences(
            self.raw_query, str(args.get("normalized_query") or ""),
        )
        args["normalized_query"] = cleaned_query
        if args.get("prefer_lightweight") and not any(
            term in self.raw_query for term in _OPTIONAL_PREFERENCE_TERMS["轻便"]
        ):
            args["prefer_lightweight"] = False
            if "轻便" not in removed_query_terms:
                removed_query_terms.append("轻便")
        preferred_type = normalize_product_type(args.get("preferred_product_type"))
        if preferred_type and preferred_type not in self._turn_types:
            args["preferred_product_type"] = None
            removed_query_terms.append("preferred_product_type")

        inferred: list[str] = []
        degraded: list[str] = []
        if args.get("product_type"):
            normalized_type = normalize_product_type(args["product_type"])
            if normalized_type:
                args["product_type"] = normalized_type
            else:
                args["product_type"] = None
                degraded.append("unknown_product_type")

        inherited: list[str] = []
        async with self._lock:
            # 已建立的类型优先于查询文字再次推断；只有首个普通检索才从词表保守推断。
            if not exact_id and not args.get("product_type") and self._constraints.get("product_type") is None:
                kind = infer_product_type(str(args.get("normalized_query") or ""), str(args.get("category") or ""))
                if kind:
                    args["product_type"] = kind
                    inferred.append("product_type")
            # 单值约束不得静默切换；明确的跨类型比较允许多个类型各自检索。
            scalar_keys = ("ship_to", "target_currency", "sku_spec") if exact_id else (
                "product_type", "ship_to", "target_currency", "sku_spec",
            )
            for key in scalar_keys:
                old, new = self._constraints.get(key), args.get(key)
                if old is not None and new is None:
                    args[key] = old
                    inherited.append(key)
                elif old is not None and new is not None and old != new:
                    if key == "product_type" and len(self._turn_types) > 1 and {old, new} <= self._turn_types:
                        continue
                    raise SearchConstraintConflict(
                        f"同一轮检索不能把硬约束 {key} 从 {old!r} 改为 {new!r}；请在新的用户请求中明确改口"
                    )

            # 上限只能收紧；模型给出更宽上限时继续使用原值。
            for key in ("price_max_major", "max_weight_kg"):
                old, new = self._constraints.get(key), args.get(key)
                if old is not None and (new is None or new > old):
                    args[key] = old
                    inherited.append(key)
                elif old is not None and new is not None:
                    args[key] = min(old, new)

            # 集合约束只允许增加，不能在改写查询时丢掉。
            for key in ("excluded_material_tags", "required_material_tags"):
                old = list(self._constraints.get(key) or [])
                merged = list(dict.fromkeys([*old, *(args.get(key) or [])]))
                if old and merged != list(args.get(key) or []):
                    inherited.append(key)
                args[key] = merged

            for key in (
                "product_type", "ship_to", "target_currency", "sku_spec",
                "price_max_major", "max_weight_kg", "excluded_material_tags", "required_material_tags",
            ):
                if exact_id and key == "product_type":
                    continue
                value = args.get(key)
                if value not in (None, [], ""):
                    self._constraints[key] = value

        return args, {
            "inherited": sorted(set(inherited)), "inferred": inferred, "degraded": degraded,
            "removed_unsupported_query_terms": sorted(removed_query_terms),
            "scope": "exact_id" if exact_id else "turn",
        }


    def cached_result(self, fingerprint: str) -> dict[str, Any] | None:
        """Return the previous result only for a byte-identical effective request."""
        if fingerprint and fingerprint == self._last_fingerprint:
            return self._last_result
        return None

    def remember_result(self, fingerprint: str, result: dict[str, Any]) -> None:
        self._last_fingerprint = fingerprint
        self._last_result = result


_current_guard: ContextVar[SearchConstraintGuard | None] = ContextVar("globex_search_constraint_guard", default=None)


class SearchConstraintScope:
    @staticmethod
    def start(raw_query: str):
        return _current_guard.set(SearchConstraintGuard(raw_query))

    @staticmethod
    def reset(token) -> None:
        _current_guard.reset(token)

    @staticmethod
    def current() -> SearchConstraintGuard | None:
        return _current_guard.get()
