"""Fail-closed checks for product claims that need catalog or knowledge evidence."""
from __future__ import annotations

import json
import re
from typing import Any

from app.domain.catalog.product_attributes import mentioned_product_types


_CLAIMS = {
    "back_support": re.compile(r"背负结构"),
    "workmanship": re.compile(r"做工(?:更)?(?:规整|精细|扎实)|做工更规整"),
    "carry_on": re.compile(r"(?:能|可|可以|适合)?登机|登机级"),
    "trip_duration": re.compile(
        r"(?:\d+\s*[-–—~～至到]\s*\d+|一两|两三|三五)\s*天(?:衣物|出行)?"
    ),
}
_QUALIFIERS = ("经验估计", "通常", "可能", "仅供参考", "需核验", "需要核验", "请核验", "以航司", "取决于")
_ESTIMATE_CLAIMS = {"carry_on", "trip_duration"}
_CONCRETE_PRODUCT_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9])P\d+(?![A-Za-z0-9])|(?:这|该|上述|以上|第[一二三四五六七八九十\d]+)[款件个]|"
    r"(?:推荐|选择|购买).{0,8}(?:商品|产品|背包|箱包|耳机|杯|鞋|衣)"
)
_PRODUCT_INTENT = re.compile(r"(?:帮我|给我|想|要|请)?.{0,8}(?:找|挑|选|推荐|比较|购买|买).{0,20}(?:商品|产品|款|背包|箱包|耳机|杯|鞋|衣|相机|电脑|手机)")
_KNOWLEDGE_ONLY = re.compile(
    r"(?:不要推荐|不用推荐|不想买|只问|仅咨询|怎么清洗|如何清洗|怎么保养|如何保养|保养方法|"
    r"方案当前可用|版本是什么|仅输出.{0,20}JSON)"
)
_PRODUCT_ID_QUERY = re.compile(r"(?<![A-Za-z0-9])P\d+(?![A-Za-z0-9])", re.IGNORECASE)


def product_claim_violations(text: str, product_result: dict[str, Any] | None) -> list[str]:
    """Return unsupported assertive claim kinds; qualified estimates are allowed."""
    if not text or not product_result:
        return []
    evidence = json.dumps(product_result.get("hits") or [], ensure_ascii=False)
    violations: list[str] = []
    for kind, pattern in _CLAIMS.items():
        for match in pattern.finditer(text):
            claim = match.group(0)
            if claim in evidence:
                continue
            left = max(0, text.rfind("。", 0, match.start()) + 1)
            right_candidates = [position for mark in "。！？\n" if (position := text.find(mark, match.end())) >= 0]
            right = min(right_candidates) if right_candidates else len(text)
            sentence = text[left:right]
            if kind in _ESTIMATE_CLAIMS and any(qualifier in sentence for qualifier in _QUALIFIERS):
                continue
            violations.append(kind)
            break
    return sorted(set(violations))


def has_concrete_product_claim(text: str) -> bool:
    """Whether a response purports to describe or recommend a concrete catalog item."""
    return bool(text and _CONCRETE_PRODUCT_REFERENCE.search(text))


def is_product_recommendation_intent(raw_query: str) -> bool:
    """Classify the buyer request, never the model's answer, for the evidence gate."""
    if not raw_query or _KNOWLEDGE_ONLY.search(raw_query):
        return False
    return bool(
        _PRODUCT_ID_QUERY.search(raw_query)
        or mentioned_product_types(raw_query)
        or _PRODUCT_INTENT.search(raw_query)
    )


def grounded_trade_fallback(turn_trade: dict[str, Any] | None) -> str:
    lines: list[str] = []
    for order in (turn_trade or {}).get("orders", []):
        if order.get("order_id"):
            lines.append(f"订单 {order['order_id']}：{order.get('status', '状态待核验')}。")
    if lines:
        return "\n".join(lines)
    return "确认决议已接收；是否执行成功请以页面确认状态和服务端订单记录为准。"


def grounded_confirmation_fallback(
    tool_names: list[str], approvals: list[bool], *,
    turn_trade: dict[str, Any] | None = None,
    memory_result: dict[str, Any] | None = None,
) -> str:
    if approvals and not any(approvals):
        return "本次操作已拒绝，未执行任何变更。"
    if turn_trade:
        return grounded_trade_fallback(turn_trade)
    if memory_result:
        action = memory_result.get("action", "变更")
        return f"长期偏好{action}已由服务端完成。"
    if tool_names and all(name in {"remember_preference_tool", "update_preference_tool", "forget_preference_tool"}
                          for name in tool_names):
        return "长期偏好确认已处理；服务端未返回可核验的写入结果，请刷新偏好列表确认。"
    return "确认决议已接收；服务端未返回可核验结果，请刷新当前状态。"


def grounded_product_fallback(
    product_result: dict[str, Any] | None,
    *,
    turn_trade: dict[str, Any] | None = None,
    confirmation: dict[str, Any] | None = None,
) -> str:
    """Render only authoritative search fields after an unsupported-claim failure."""
    hits = (product_result or {}).get("hits") or []
    if not hits:
        return "本轮没有足够的可核验商品结果，请调整筛选条件后再试。"
    lines = ["以下为目录工具返回的可核验商品信息："]
    for hit in hits[:5]:
        title = str(hit.get("title") or "未命名商品")
        product_id = str(hit.get("product_id") or "未知编号")
        price = hit.get("price_major")
        currency = str(hit.get("currency") or "").strip()
        price_text = f"，商品价 {price} {currency}" if price is not None else ""
        lines.append(f"• {title}（{product_id}）{price_text}。")
    lines.append("如需比较容量、重量或是否适合登机，请以商品详情和承运方规则为准。")
    for order in (turn_trade or {}).get("orders", []):
        if order.get("order_id"):
            lines.append(f"本轮订单 {order['order_id']}：{order.get('status', '状态待核验')}。")
    if (confirmation or {}).get("pending"):
        lines.append("本轮已生成待确认操作；尚未执行，请在页面确认卡核对并选择。")
    return "\n".join(lines)
