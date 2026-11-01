"""最终商品回复只能使用已校验工具事实的强制契约。"""
from types import SimpleNamespace

from app.application.agents.orchestrator import MainAgentOrchestrator
from app.application.answer_grounding import (
    has_concrete_product_claim,
    grounded_confirmation_fallback,
    grounded_product_fallback,
    grounded_trade_fallback,
    is_product_recommendation_intent,
    product_claim_violations,
)
from app.infrastructure.budget import init_budget, remember_verified_result


def test_product_claim_guard_rejects_assertions_without_evidence_but_allows_scoped_estimates():
    products = {"hits": [{"product_id": "P1", "title": "旅行背包 30L", "weight_kg": 0.7}]}
    bad = "这款有背负结构，做工更规整，能登机，可以装 3–5 天衣物。"
    assert product_claim_violations(bad, products) == [
        "back_support", "carry_on", "trip_duration", "workmanship",
    ]
    qualified = "30L 通常可作为短途容量参考，但可否登机需按航司和舱位核验，仅供参考。"
    assert product_claim_violations(qualified, products) == []
    assert product_claim_violations("这款有背负结构，但需核验。", products) == ["back_support"]


def test_orchestrator_replaces_unsupported_claims_with_verified_catalog_facts():
    init_budget(0)
    remember_verified_result("products", {"hits": [{
        "product_id": "P1", "title": "旅行背包 30L", "price_major": 129, "currency": "CNY",
    }]})
    orchestrator = object.__new__(MainAgentOrchestrator)
    orchestrator._bus = SimpleNamespace(publish=lambda *args, **kwargs: None)
    orchestrator._output_guard_enabled = False

    guarded = orchestrator._guard_final_text("s1", "这款有背负结构，做工更规整，能登机。")
    assert "背负结构" not in guarded and "做工" not in guarded and "能登机" not in guarded
    assert "旅行背包 30L" in guarded and "P1" in guarded and "129 CNY" in guarded
    assert "Token 预算" not in guarded

    # 即使模型完全换一种说法、或把 P1 的属性嫁接给 P2，生产接线也只输出 hits 白名单。
    crossed = orchestrator._guard_final_text(
        "s1", "P2 背负系统很好，工艺精良且结实耐用，可作为航空随身行李，适合一周旅行。",
    )
    assert "P2" not in crossed and "工艺精良" not in crossed and crossed == guarded

    # 历史订单或历史待确认不能关闭新一轮商品 grounding。
    remember_verified_result("trade", {"orders": [{"order_id": "OLD"}]})
    assert orchestrator._guard_final_text("s1", "城市漫步 20L 很适合你，工艺精良。") == guarded
    remember_verified_result("confirmation", {"pending": True})
    pending = orchestrator._guard_final_text("s1", "首选城市漫步系列，背负系统很好。")
    assert "背负系统" not in pending and "本轮已生成待确认操作" in pending


def test_orchestrator_fails_closed_for_concrete_product_claim_without_tool_evidence():
    init_budget(0)
    orchestrator = object.__new__(MainAgentOrchestrator)
    orchestrator._bus = SimpleNamespace(publish=lambda *args, **kwargs: None)
    orchestrator._output_guard_enabled = False

    assert has_concrete_product_claim("P2 工艺精良，适合一周旅行。")
    guarded = orchestrator._guard_final_text("s1", "P2 工艺精良，适合一周旅行。")
    assert "P2" not in guarded
    assert guarded == "本轮没有足够的可核验商品结果，请调整筛选条件后再试。"

    assert is_product_recommendation_intent("帮我挑一个适合旅行的背包")
    for query in (
        "我需要一个20元内送中国的背包", "20元以内配送中国的背包", "旅行背包，预算300",
        "有没有适合旅行的背包", "给我看看背包", "P1001怎么样",
    ):
        assert is_product_recommendation_intent(query), query
    for query in ("不要推荐商品，只问背包怎么清洗", "我不想买背包，只想问保养"):
        assert not is_product_recommendation_intent(query), query
    implicit = orchestrator._guard_final_text(
        "s1", "城市漫步 20L 很适合你，工艺精良。", require_product_evidence=True,
    )
    assert implicit == guarded
    assert grounded_trade_fallback({"orders": [{"order_id": "GBX-1", "status": "created"}]}) == (
        "订单 GBX-1：created。"
    )
    assert "是否执行成功" in grounded_trade_fallback(None)
    assert grounded_confirmation_fallback(
        ["remember_preference_tool"], [False],
    ) == "本次操作已拒绝，未执行任何变更。"
    assert "长期偏好保存" in grounded_confirmation_fallback(
        ["remember_preference_tool"], [True], memory_result={"action": "保存"},
    )
    assert "订单" not in grounded_confirmation_fallback(["remember_preference_tool"], [True])


def test_product_fallback_reports_landed_price_with_fee_breakdown():
    payload = {"hits": [{
        "product_id": "P1049", "title": "BudgetPack 20L 简易背包",
        "price_major": 39, "currency": "CNY",
        "landed_price": {"subtotal_major": 39, "freight_major": 25, "tariff_major": 0,
                         "landed_total_major": 64, "currency": "CNY"},
    }]}

    text = grounded_product_fallback(payload)

    assert "商品价 39 CNY" in text
    assert "到手价 64 CNY" in text
    assert "运费 25" in text and "关税 0" in text


def test_product_fallback_does_not_invent_landed_price_without_evidence():
    payload = {"hits": [{
        "product_id": "P1", "title": "旅行背包 30L", "price_major": 129, "currency": "CNY",
    }]}

    text = grounded_product_fallback(payload)

    assert "商品价 129 CNY" in text
    assert "到手价" not in text
