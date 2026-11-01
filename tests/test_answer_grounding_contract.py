"""最终商品回复只能使用已校验工具事实的强制契约。"""
from types import SimpleNamespace

from app.application.agents.orchestrator import MainAgentOrchestrator
from app.application.answer_grounding import has_concrete_product_claim, product_claim_violations
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


def test_orchestrator_fails_closed_for_concrete_product_claim_without_tool_evidence():
    init_budget(0)
    orchestrator = object.__new__(MainAgentOrchestrator)
    orchestrator._bus = SimpleNamespace(publish=lambda *args, **kwargs: None)
    orchestrator._output_guard_enabled = False

    assert has_concrete_product_claim("P2 工艺精良，适合一周旅行。")
    guarded = orchestrator._guard_final_text("s1", "P2 工艺精良，适合一周旅行。")
    assert "P2" not in guarded
    assert guarded == "本轮没有足够的可核验商品结果，请调整筛选条件后再试。"
