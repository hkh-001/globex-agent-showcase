# -*- coding: utf-8 -*-
"""检索改写不得静默放宽本轮已经建立的业务硬约束。"""
import asyncio
import json

from app.application.agents.product_candidate_projection import ProductCandidateProjection
from app.application.search_constraint_guard import SearchConstraintConflict, SearchConstraintGuard, SearchConstraintScope
from app.application.tools.product_search_tool import build_product_search_tool
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEventBus, observe_run_events


def _args(**overrides):
    values = {
        "normalized_query": "背包", "category": "旅行装备", "ship_to": "CN", "top_k": 10,
        "price_max_major": 300.0, "target_currency": "CNY", "excluded_material_tags": [],
        "required_material_tags": [], "product_id": None, "sku_id": None, "sku_spec": None,
        "product_type": "背包", "max_weight_kg": None, "preferred_product_type": None,
        "prefer_lightweight": True,
    }
    values.update(overrides)
    return values


async def test_rewrite_inherits_type_budget_destination_and_material_constraints():
    guard = SearchConstraintGuard("找一个寄到中国、300元内的轻便背包")
    first, _ = await guard.reconcile(_args(excluded_material_tags=["合成聚合物"]))
    second, metadata = await guard.reconcile(_args(
        normalized_query="石墨黑 登山背包 轻量 短途", product_type=None, ship_to=None,
        price_max_major=None, excluded_material_tags=[],
    ))
    assert first["product_type"] == second["product_type"] == "背包"
    assert second["ship_to"] == "CN" and second["price_max_major"] == 300.0
    assert second["excluded_material_tags"] == ["合成聚合物"]
    assert set(metadata["inherited"]) >= {"product_type", "ship_to", "price_max_major", "excluded_material_tags"}


async def test_known_query_type_is_inferred_but_unknown_type_degrades_without_guessing():
    known, known_meta = await SearchConstraintGuard("找背包").reconcile(_args(product_type=None))
    unknown, unknown_meta = await SearchConstraintGuard("找保温杯").reconcile(
        _args(normalized_query="旅行保温杯", product_type=None, category="户外运动")
    )
    assert known["product_type"] == "背包" and known_meta["inferred"] == ["product_type"]
    assert unknown["product_type"] is None and unknown_meta["inferred"] == []


async def test_unstated_preferences_cannot_silently_change_retrieval_ranking():
    effective, metadata = await SearchConstraintGuard("帮我挑一个适合旅行的背包").reconcile(
        _args(normalized_query="旅行背包 登机 轻便 大容量", prefer_lightweight=True,
              preferred_product_type="耳机")
    )
    assert effective["normalized_query"] == "旅行背包"
    assert effective["prefer_lightweight"] is False
    assert effective["preferred_product_type"] is None
    assert metadata["removed_unsupported_query_terms"] == ["preferred_product_type", "大容量", "登机", "轻便"]

    explicit, explicit_meta = await SearchConstraintGuard("找一个轻便、可登机的大容量背包").reconcile(
        _args(normalized_query="轻量 登机 容量大 背包", prefer_lightweight=True)
    )
    assert explicit["normalized_query"] == "轻量 登机 容量大 背包"
    assert explicit["prefer_lightweight"] is True
    assert explicit_meta["removed_unsupported_query_terms"] == []


async def test_unknown_explicit_type_degrades_without_guessing_or_error():
    effective, metadata = await SearchConstraintGuard("找保温杯").reconcile(
        _args(normalized_query="旅行保温杯", product_type="保温杯", category="户外运动")
    )
    assert effective["product_type"] is None
    assert metadata["degraded"] == ["unknown_product_type"]


async def test_scope_propagates_to_child_tasks_and_new_turn_resets_it():
    token = SearchConstraintScope.start("找背包")
    try:
        parent = SearchConstraintScope.current()
        child = await asyncio.create_task(asyncio.to_thread(SearchConstraintScope.current))
        assert child is parent
    finally:
        SearchConstraintScope.reset(token)
    assert SearchConstraintScope.current() is None
    next_token = SearchConstraintScope.start("改看保温杯")
    try:
        assert SearchConstraintScope.current() is not parent
    finally:
        SearchConstraintScope.reset(next_token)


async def test_explicit_cross_type_comparison_is_allowed_and_exact_ids_are_exempt():
    guard = SearchConstraintGuard("比较背包和颈枕")
    await guard.reconcile(_args(product_type="背包"))
    changed, _ = await guard.reconcile(_args(normalized_query="旅行颈枕", product_type="颈枕"))
    exact, metadata = await guard.reconcile(_args(product_id="P1001", normalized_query=""))
    assert changed["product_type"] == "颈枕"
    assert exact["product_id"] == "P1001" and metadata["scope"] == "exact_id"
    assert exact["ship_to"] == "CN" and exact["price_max_major"] == 300.0


async def test_exact_id_type_does_not_pollute_following_ordinary_search_baseline():
    guard = SearchConstraintGuard("找背包并核对 P1001")
    await guard.reconcile(_args(product_type="背包"))
    await guard.reconcile(_args(normalized_query="", product_id="P1001", product_type="颈枕"))
    following, _ = await guard.reconcile(_args(normalized_query="换一组背包", product_type="背包"))
    assert following["product_type"] == "背包"


async def test_unannounced_type_switch_in_same_turn_is_rejected_instead_of_broadening():
    guard = SearchConstraintGuard("找一个背包")
    await guard.reconcile(_args(product_type="背包"))
    import pytest
    with pytest.raises(SearchConstraintConflict, match="不能把硬约束 product_type"):
        await guard.reconcile(_args(normalized_query="旅行颈枕", product_type="颈枕"))


def test_projection_rejects_later_batch_that_drops_hard_type():
    projection = ProductCandidateProjection("找背包")
    first = {"tool": "product_search_tool", "hits": [{"product_id": "P1"}],
             "recall_strategy": "keyword_2gram", "query_conditions": {"product_type": "背包"}}
    broad = {"tool": "product_search_tool", "hits": [{"product_id": "P2"}],
             "recall_strategy": "keyword_2gram", "query_conditions": {}}
    assert projection.apply(first) is True
    assert projection.apply(broad) is False
    assert projection.result["hits"][0]["product_id"] == "P1"


def test_projection_accepts_explicit_cross_type_comparison():
    projection = ProductCandidateProjection("比较背包和颈枕")
    backpack = {"tool": "product_search_tool", "hits": [{"product_id": "P1"}],
                "recall_strategy": "keyword_2gram", "query_conditions": {"product_type": "背包"}}
    pillow = {"tool": "product_search_tool", "hits": [{"product_id": "P2"}],
              "recall_strategy": "keyword_2gram", "query_conditions": {"product_type": "颈枕"}}
    assert projection.apply(backpack) is True
    assert projection.apply(pillow) is True
    assert projection.result["hits"][0]["product_id"] == "P2"


async def test_tool_events_and_projection_share_cross_type_policy():
    class UseCase:
        async def execute(self, spec):
            return {"hits": [{"product_id": "P1" if spec.product_type == "背包" else "P2"}],
                    "recall_strategy": "keyword_2gram", "total_candidates": 1, "rerank_applied": False}
    projection = ProductCandidateProjection("比较背包和颈枕")
    bus = TradeEventBus()
    tool = build_product_search_tool(UseCase(), bus)
    context_token = ShoppingContext.set(ShoppingContextSnapshot("s", "b", "zh-CN", "CNY"))
    guard_token = SearchConstraintScope.start("比较背包和颈枕")
    try:
        with observe_run_events(lambda event: projection.apply(event.payload) if event.type == "tool.result" else None):
            await tool(normalized_query="背包", product_type="背包")
            await tool(normalized_query="颈枕", product_type="颈枕")
    finally:
        SearchConstraintScope.reset(guard_token)
        ShoppingContext.reset(context_token)
    assert projection.result["hits"][0]["product_id"] == "P2"


async def test_tool_projection_accepts_single_exact_id_without_losing_ordinary_type_baseline():
    class UseCase:
        async def execute(self, spec):
            product = spec.product_id or "P-BACKPACK"
            hits = [] if spec.product_id == "P9999" else [{"product_id": product}]
            return {"hits": hits, "recall_strategy": "exact_id_lookup" if spec.product_id else "keyword_2gram",
                    "total_candidates": len(hits), "rerank_applied": False}
    projection = ProductCandidateProjection("找背包并核对 P1001，再确认 P9999 是否存在")
    bus = TradeEventBus()
    tool = build_product_search_tool(UseCase(), bus)
    context_token = ShoppingContext.set(ShoppingContextSnapshot("s", "b", "zh-CN", "CNY"))
    guard_token = SearchConstraintScope.start("找背包并核对 P1001，再确认 P9999 是否存在")
    try:
        with observe_run_events(lambda event: projection.apply(event.payload) if event.type == "tool.result" else None):
            await tool(normalized_query="背包", product_type="背包", ship_to="CN")
            await tool(normalized_query="", product_id="P1001")
            assert projection.result["hits"][0]["product_id"] == "P1001"
            await tool(normalized_query="", product_id="P9999")
            assert projection.result["hits"] == []
            await tool(normalized_query="换一组背包", product_type="背包")
    finally:
        SearchConstraintScope.reset(guard_token)
        ShoppingContext.reset(context_token)
    assert projection.result["hits"][0]["product_id"] == "P-BACKPACK"


def test_multi_exact_merge_rejects_broader_late_result_and_clears_cards_when_tightened():
    projection = ProductCandidateProjection("比较 P1001 和 P1002")
    def payload(product, budget, ship="CN"):
        return {"tool": "product_search_tool", "hits": [{"product_id": product}],
                "recall_strategy": "exact_id_lookup",
                "query_conditions": {"product_id": product, "price_max_major": budget, "ship_to": ship}}
    assert projection.apply(payload("P1001", 300)) is True
    assert projection.apply(payload("P1002", 100)) is True
    assert [hit["product_id"] for hit in projection.result["hits"]] == ["P1002"]
    # 并行中较宽的旧调用后完成时不得把已收紧的结果重新放宽。
    assert projection.apply(payload("P1001", 300)) is False
    assert [hit["product_id"] for hit in projection.result["hits"]] == ["P1002"]
    # 丢掉目的国同样不能合并。
    assert projection.apply(payload("P1001", 100, ship=None)) is False


def test_multi_exact_first_new_constraint_clears_unverified_old_cards():
    projection = ProductCandidateProjection("比较 P1001 和 P1002")
    unconstrained = {"tool": "product_search_tool", "hits": [{"product_id": "P1001"}],
                     "recall_strategy": "exact_id_lookup", "query_conditions": {"product_id": "P1001"}}
    constrained = {"tool": "product_search_tool", "hits": [{"product_id": "P1002"}],
                   "recall_strategy": "exact_id_lookup",
                   "query_conditions": {"product_id": "P1002", "price_max_major": 100,
                                        "max_weight_kg": 0.5, "required_material_tags": ["棉"]}}
    assert projection.apply(unconstrained) is True
    assert projection.apply(constrained) is True
    assert [hit["product_id"] for hit in projection.result["hits"]] == ["P1002"]
    # 无约束的旧并行结果迟到时不能恢复。
    assert projection.apply(unconstrained) is False


async def test_tool_publishes_effective_conditions_and_does_not_pad_to_top_k():
    seen_specs = []
    class UseCase:
        async def execute(self, spec):
            seen_specs.append(spec)
            return {"hits": [{"product_id": "P1", "title": "背包"}], "recall_strategy": "keyword_2gram",
                    "total_candidates": 1, "rerank_applied": False}

    events = []
    bus = TradeEventBus()
    tool = build_product_search_tool(UseCase(), bus)
    context_token = ShoppingContext.set(ShoppingContextSnapshot("s", "b", "zh-CN", "CNY"))
    guard_token = SearchConstraintScope.start("找背包")
    try:
        with observe_run_events(events.append):
            await tool(normalized_query="背包", product_type="背包", ship_to="CN", top_k=10)
            reply = await tool(normalized_query="石墨黑 登山背包", top_k=10)
    finally:
        SearchConstraintScope.reset(guard_token)
        ShoppingContext.reset(context_token)
    payloads = [event.payload for event in events if event.type == "tool.result" and "hits" in event.payload]
    assert payloads[-1]["query_conditions"]["product_type"] == "背包"
    assert seen_specs[-1].product_type == "背包"
    assert len(payloads[-1]["hits"]) == 1  # top_k 是上限，不为凑数补无关商品。
    assert json.loads(reply.content[0].text)["hits"][0]["product_id"] == "P1"


async def test_identical_effective_request_reuses_previous_result_within_turn():
    calls: list[str] = []

    class UseCase:
        async def execute(self, spec):
            calls.append(spec.normalized_query)
            return {"hits": [{"product_id": "P1"}], "recall_strategy": "keyword_2gram",
                    "total_candidates": 1, "rerank_applied": False}

    events = []
    tool = build_product_search_tool(UseCase(), TradeEventBus())
    context_token = ShoppingContext.set(ShoppingContextSnapshot("s", "b", "zh-CN", "CNY"))
    guard_token = SearchConstraintScope.start("找背包")
    try:
        with observe_run_events(events.append):
            await tool(normalized_query="背包", product_type="背包", ship_to="CN", top_k=5)
            await tool(normalized_query="背包", product_type="背包", ship_to="CN", top_k=5)
            # 只有 top_k 不同：是不同请求，必须重新检索
            await tool(normalized_query="背包", product_type="背包", ship_to="CN", top_k=4)
    finally:
        SearchConstraintScope.reset(guard_token)
        ShoppingContext.reset(context_token)
    assert calls == ["背包", "背包"]

    with_results = [e for e in events if e.type == "tool.result" and e.payload.get("hits")]
    assert len(with_results) == 3
    # 复用必须原样返回同一批候选，不能丢 hits 或换商品
    assert with_results[0].payload["hits"] == with_results[1].payload["hits"]
    # 复用不刷新观测时间：它是首次观测的时刻
    assert with_results[0].payload.get("observed_at") == with_results[1].payload.get("observed_at")
    assert with_results[1].payload["reused_duplicate"] is True
    assert with_results[2].payload["reused_duplicate"] is False


async def test_new_turn_does_not_reuse_previous_result():
    calls: list[str] = []

    class UseCase:
        async def execute(self, spec):
            calls.append(spec.normalized_query)
            return {"hits": [{"product_id": "P1"}], "recall_strategy": "keyword_2gram",
                    "total_candidates": 1, "rerank_applied": False}

    tool = build_product_search_tool(UseCase(), TradeEventBus())
    context_token = ShoppingContext.set(ShoppingContextSnapshot("s", "b", "zh-CN", "CNY"))
    try:
        for _ in range(2):
            guard_token = SearchConstraintScope.start("找背包")
            try:
                await tool(normalized_query="背包", product_type="背包", ship_to="CN", top_k=5)
            finally:
                SearchConstraintScope.reset(guard_token)
    finally:
        ShoppingContext.reset(context_token)
    assert calls == ["背包", "背包"]


def test_request_fingerprint_is_derived_from_the_normalized_spec():
    from dataclasses import fields, replace

    from app.application.tools.product_search_tool import request_fingerprint
    from app.domain.catalog.product_search_spec import ProductSearchSpec

    base = ProductSearchSpec(normalized_query="背包")
    covered = set(json.loads(request_fingerprint(base)))
    assert covered == {spec_field.name for spec_field in fields(ProductSearchSpec)}
    # locale 不再恒为 null：它来自 spec，而不是工具 args
    assert json.loads(request_fingerprint(base))["locale"] == "zh-CN"
    # 归一化后再取指纹：大小写不同的 product_id 视为同一请求
    assert request_fingerprint(
        ProductSearchSpec(normalized_query="", product_id="p1001")
    ) == request_fingerprint(ProductSearchSpec(normalized_query="", product_id="P1001"))
    # 任一约束变化必须改变指纹
    assert request_fingerprint(replace(base, ship_to="US")) != request_fingerprint(base)
    assert request_fingerprint(replace(base, top_k=4)) != request_fingerprint(base)


async def test_exact_id_detail_does_not_inherit_a_search_sku_spec():
    seen = []

    class UseCase:
        async def execute(self, spec):
            seen.append(spec)
            return {"hits": [{"product_id": spec.product_id or "P1"}],
                    "recall_strategy": "exact_id_lookup" if spec.product_id else "keyword_2gram",
                    "total_candidates": 1, "rerank_applied": False}

    tool = build_product_search_tool(UseCase(), TradeEventBus())
    context_token = ShoppingContext.set(ShoppingContextSnapshot("s", "b", "zh-CN", "CNY"))
    guard_token = SearchConstraintScope.start("300 元以内、送到中国、黑色旅行背包")
    try:
        await tool(normalized_query="旅行背包", product_type="背包", sku_spec="黑色", ship_to="CN")
        await tool(normalized_query="", product_id="P1003")
    finally:
        SearchConstraintScope.reset(guard_token)
        ShoppingContext.reset(context_token)

    assert seen[0].sku_spec == "黑色"
    # 详情查询要能看到该商品的全部可售规格，不能被上一轮的“黑色”卡死
    assert seen[1].product_id == "P1003"
    assert seen[1].sku_spec is None
    assert seen[1].ship_to == "CN"
