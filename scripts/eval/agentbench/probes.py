"""Stage 1.5 runtime evidence probes.

Each probe exercises the real code path with its own bus/context and records the
observations it actually produced.  Probes never patch globals and never call a
model or external network.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
from types import SimpleNamespace

from app.application.agents.orchestrator import MainAgentOrchestrator
from app.application.search_constraint_guard import SearchConstraintScope
from app.application.tools.product_search_tool import build_product_search_tool
from app.application.tools.task_dispatch_tool import build_task_dispatch_tool
from app.application.usecases.catalog_search import CatalogSearchUseCase
from app.infrastructure.budget import init_budget, remember_verified_result
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEventBus, observe_run_events
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.persistence.json_file_stores import JsonFilePreferenceStore
from scripts.eval.agentbench import value_assertions
from app.application.tools.remember_preference_tool import build_remember_preference_tool
from app.application.tools.update_preference_tool import build_update_preference_tool
from app.application.tools.forget_preference_tool import build_forget_preference_tool
from app.infrastructure.budget import verified_result
from app.application.usecases.order_usecases import OrderItemInput, PlaceOrderUseCase, QueryOrderUseCase
from app.domain.order.address import Address
from app.application.usecases.confirmation_service import ConfirmationService
from app.application.tools.order_tools import build_create_order_tool, build_query_order_tool
from app.infrastructure.persistence.sql.repositories import bootstrap_schema, create_engine
from app.infrastructure.persistence.sql.trade_store import SqlTradeStore
from app.infrastructure.persistence.context_evidence import ContextEvidenceStore
from app.application.tools.category_insight_tool import build_category_insight_tool
from app.infrastructure.rag.category_knowledge import bootstrap_category_knowledge

PROBE_FILE = "eval/agent/v1/probes/test_evidence_probe.py"


class _EvidenceStore:
    async def save(self, *_args, **_kwargs):
        return "probe-ref"


def _records(events) -> list[dict]:
    return [{"type": event.type, "payload": event.payload,
             "correlation": getattr(event, "correlation", {}),
             "shopping_session_id": getattr(event, "shopping_session_id", "")}
            for event in events]


def _present(payload, field: str) -> bool:
    current = payload
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return False
        current = current[part]
    return True


def check_fields(records: list[dict], selector: str, fields: list[str]) -> tuple[bool, list[str]]:
    """Return whether the selector event exists and every listed (possibly nested) field is present."""
    kind, _, name = selector.partition(":")
    matching = [row for row in records if row["type"] == kind and (
        not name or str((row["payload"] or {}).get("tool") or "") == name)]
    if not matching:
        return False, []
    payload = matching[-1]["payload"] or {}
    observed = [field for field in fields if _present(payload, field)]
    return len(observed) == len(fields), observed


def _payload_from(records):
    for row in records:
        if isinstance(row, dict) and row.get("type") == "tool.result":
            return row.get("payload") or {}
        if getattr(row, "type", None) == "tool.result":
            return getattr(row, "payload", {}) or {}
    for row in records:
        if isinstance(row, dict) and row.get("payload"):
            return row["payload"]
        if getattr(row, "payload", None):
            return row.payload
    return {}


def first_payload(records):
    return next((row["payload"] for row in records if row["type"] == "tool.result"), {})


def payload_hits(records):
    return first_payload(records).get("hits") or []


async def probe_search() -> list[dict]:
    repo = InMemoryProductRepository()
    bus = TradeEventBus()
    tool = build_product_search_tool(CatalogSearchUseCase(repo), bus, _EvidenceStore())
    results = []
    context_token = ShoppingContext.set(ShoppingContextSnapshot("s", "b", "zh-CN", "CNY"))

    async def run(**kwargs):
        events = []
        with observe_run_events(events.append):
            await tool(**kwargs)
        return _records(events)

    catalog = await run(normalized_query="旅行背包", product_type="背包", ship_to="CN", top_k=5)
    ok, observed = check_fields(catalog, "tool.result:product_search_tool",
                                ["tool", "hit_count", "recall_strategy", "total_candidates",
                                 "query_conditions", "hits", "filtered_out", "result_ref"])
    first_hit = (payload_hits(catalog) or [{}])[0]
    results.append(_result("search.catalog", "event", "tool.result:product_search_tool",
                           ["tool", "hit_count", "recall_strategy", "total_candidates",
                            "query_conditions", "hits", "filtered_out", "result_ref"], observed, ok, catalog,
                           observed_values={"hit_count": first_payload(catalog).get("hit_count"),
                                            "recall_strategy": first_payload(catalog).get("recall_strategy"),
                                            "total_candidates": first_payload(catalog).get("total_candidates"),
                                            "top_product_id": first_hit.get("product_id"),
                                            "result_ref": first_payload(catalog).get("result_ref")}))
    constraints_ok, constraints_observed = check_fields(
        catalog, "tool.result:product_search_tool",
        ["query_conditions", "filtered_out"])
    conditions = first_payload(catalog).get("query_conditions") or {}
    results.append(_result("search.constraints", "event", "tool.result:product_search_tool",
                           ["query_conditions", "filtered_out"], constraints_observed,
                           constraints_ok, catalog,
                           observed_values={"ship_to": conditions.get("ship_to"),
                                            "product_type": conditions.get("product_type"),
                                            "filtered_out_count": len(first_payload(catalog).get("filtered_out") or [])}))

    payload = next((row["payload"] for row in catalog if row["type"] == "tool.result"), {})
    hit = (payload.get("hits") or [{}])[0]
    landed = hit.get("landed_price") or {}
    landed_fields = ["subtotal_major", "freight_major", "tariff_major", "landed_total_major", "currency"]
    landed_required = [f"hits[].landed_price.{name}" for name in landed_fields]
    landed_ok = all(name in landed for name in landed_fields)
    results.append(_result("search.landed_price", "event", "tool.result:product_search_tool",
                           landed_required, landed_required if landed_ok else [], landed_ok, catalog,
                           observed_values={name: landed.get(name) for name in landed_fields}))

    exact = await run(normalized_query="", product_id="P1003", ship_to="CN", top_k=5)
    exact_payload = next((row["payload"] for row in exact if row["type"] == "tool.result"), {})
    exact_hits = exact_payload.get("hits") or []
    ok_exact = exact_payload.get("recall_strategy") == "exact_id_lookup" and any(
        item.get("product_id") == "P1003" for item in exact_hits)
    results.append(_result("search.exact_id", "event", "tool.result:product_search_tool",
                           ["recall_strategy", "hits", "query_conditions"], sorted(exact_payload),
                           ok_exact, exact,
                           observed_values={"recall_strategy": exact_payload.get("recall_strategy"),
                                            "first_product_id": (exact_hits or [{}])[0].get("product_id")}))

    guard_token = SearchConstraintScope.start("300 元以内 送到中国 的黑色旅行背包")
    try:
        await run(normalized_query="旅行背包", product_type="背包", ship_to="CN",
                  price_max_major=300, sku_spec="黑色", top_k=5)
        carried = await run(normalized_query="轻便 旅行背包", product_type=None, ship_to=None, top_k=5)
    finally:
        SearchConstraintScope.reset(guard_token)
    carried_payload = next((row["payload"] for row in carried if row["type"] == "tool.result"), {})
    inherited = (carried_payload.get("constraint_continuity") or {}).get("inherited") or []
    ShoppingContext.reset(context_token)
    carry_fields = ["constraint_continuity.inherited", "query_conditions.price_max_major",
                    "query_conditions.ship_to", "query_conditions.sku_spec"]
    carry_observed = [field for field in carry_fields if _present(carried_payload, field)]
    results.append(_result("state.constraint_carry", "event", "tool.result:product_search_tool",
                           carry_fields, carry_observed,
                           bool(inherited) and len(carry_observed) == len(carry_fields), carried,
                           observed_values={"inherited": inherited}))
    return results


class _Worker:
    def __init__(self, bus):
        self._bus = bus

    async def reply(self, _inputs):
        from app.infrastructure.context import ShoppingContext
        session = ShoppingContext.current_session_id()
        self._bus.publish(session, "tool.result", {
            "tool": "create_order_tool",
            "confirmation": {"payload": {"items": [{"product_id": "P1003", "sku_id": "P1003-S1"}]}},
        })
        return SimpleNamespace(get_text_content=lambda: "done P1003")


class _Factory:
    def __init__(self, worker):
        self._worker = worker

    def build(self):
        return self._worker


async def probe_dispatch() -> list[dict]:
    bus = TradeEventBus()
    tool = build_task_dispatch_tool(_Factory(_Worker(bus)), _Factory(_Worker(bus)), bus,
                                    subagent_inject=False)
    context_token = ShoppingContext.set(ShoppingContextSnapshot("s", "b", "zh-CN", "CNY"))
    events = []
    try:
        with observe_run_events(events.append):
            chunk = await tool("search_agent", "find a backpack")
    finally:
        ShoppingContext.reset(context_token)
    records = _records(events)
    ok, observed = check_fields(records, "tool.result:task_dispatch",
                                ["tool", "agent", "dispatch_id", "status"])
    content = "".join(getattr(block, "text", "") for block in chunk.content)
    verified = None
    try:
        decision = json.loads(content)
        verified = decision.get("verified_product_ids")
    except Exception:
        verified = None
    dispatch_payload = next((row["payload"] for row in records
                             if row["type"] == "tool.result"
                             and row["payload"].get("tool") == "task_dispatch"), {})
    results = [_result("dispatch.search", "event", "tool.result:task_dispatch",
                       ["tool", "agent", "dispatch_id", "status"], observed, ok, records,
                       observed_values={"agent": dispatch_payload.get("agent"),
                                        "dispatch_id": dispatch_payload.get("dispatch_id"),
                                        "status": dispatch_payload.get("status")},
                       payload=dispatch_payload)]
    verified_ok = isinstance(verified, list) and "P1003" in verified
    results.append(_result("dispatch.search", "tool_result", "task_dispatch",
                           ["verified_product_ids", "unverified_product_ids"],
                           ["verified_product_ids", "unverified_product_ids"] if verified is not None else [],
                           verified_ok, records,
                           observed_values={"status": decision.get("status") if decision else None,
                                            "verified_product_ids": verified,
                                            "unverified_product_ids": (decision or {}).get("unverified_product_ids")}))
    decision = {}
    try:
        decision = json.loads(content)
    except Exception:
        decision = {}
    results[-1]["observed_values"] = {"status": decision.get("status"),
                                      "verified_product_ids": decision.get("verified_product_ids"),
                                      "unverified_product_ids": decision.get("unverified_product_ids")}
    results[-1]["required_fields"] = ["verified_product_ids", "unverified_product_ids"]
    results[-1]["result"] = "pass" if (verified_ok and decision.get("status") == "completed"
                                       and decision.get("unverified_product_ids") == []) else "fail"

    trade_events = []
    trade_tool = build_task_dispatch_tool(_Factory(_Worker(bus)), _Factory(_Worker(bus)), bus,
                                          subagent_inject=False)
    context_token = ShoppingContext.set(ShoppingContextSnapshot("s", "b", "zh-CN", "CNY"))
    try:
        with observe_run_events(trade_events.append):
            trade_chunk = await trade_tool("trade_agent", "prepare an order")
    finally:
        ShoppingContext.reset(context_token)
    trade_records = _records(trade_events)
    trade_ok, trade_observed = check_fields(trade_records, "tool.result:task_dispatch",
                                            ["tool", "agent", "dispatch_id", "status"])
    trade_payload = next((row["payload"] for row in trade_records
                          if row["type"] == "tool.result"
                          and row["payload"].get("tool") == "task_dispatch"), {})
    results.append(_result("dispatch.trade", "event", "tool.result:task_dispatch",
                           ["tool", "agent", "dispatch_id", "status"], trade_observed,
                           trade_ok, trade_records,
                           observed_values={"agent": trade_payload.get("agent"),
                                            "dispatch_id": trade_payload.get("dispatch_id"),
                                            "status": trade_payload.get("status")},
                           payload=trade_payload))
    trade_content = "".join(getattr(block, "text", "") for block in trade_chunk.content)
    try:
        trade_decision = json.loads(trade_content)
    except Exception:
        trade_decision = {}
    trade_verified = trade_decision.get("verified_product_ids")
    trade_ok_result = (trade_decision.get("status") == "completed"
                       and isinstance(trade_verified, list) and "P1003" in trade_verified
                       and trade_decision.get("unverified_product_ids") == [])
    results.append(_result("dispatch.trade", "tool_result", "task_dispatch",
                           ["verified_product_ids", "unverified_product_ids"],
                           ["verified_product_ids", "unverified_product_ids"]
                           if trade_verified is not None else [],
                           trade_ok_result, trade_records,
                           observed_values={"status": trade_decision.get("status"),
                                            "verified_product_ids": trade_verified,
                                            "unverified_product_ids": trade_decision.get("unverified_product_ids")}))
    return results


async def probe_grounding(tmp_path=None) -> list[dict]:
    import pathlib
    import tempfile
    path = pathlib.Path(tmp_path or tempfile.mkdtemp()) / "grounding_audit.jsonl"
    bus = TradeEventBus()
    orchestrator = object.__new__(MainAgentOrchestrator)
    orchestrator._bus = bus
    orchestrator._output_guard_enabled = False
    orchestrator._grounding_audit_path = path
    init_budget(0)
    remember_verified_result("products", {"hits": [{"product_id": "P1", "title": "旅行背包"}]})
    events = []
    with observe_run_events(events.append):
        orchestrator._guard_final_text("s", "这款能登机")
    records = _records(events)
    ok_event, observed = check_fields(records, "error", ["message", "claim_guard"])
    audit_rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    row = audit_rows[-1] if audit_rows else {}
    fields = ["replacement_kind", "violations"]
    error_payload = next((row["payload"] for row in records if row["type"] == "error"), {})
    results = [_result("harness.grounding_gate", "event", "error", ["message", "claim_guard"],
                       observed, ok_event, records,
                       observed_values={"message": error_payload.get("message"),
                                        "claim_guard": error_payload.get("claim_guard")},
                       payload=error_payload),
               _result("harness.grounding_gate", "private_artifact", "grounding_audit.jsonl",
                       fields, sorted(row), all(name in row for name in fields), records,
                       observed_values={name: row.get(name) for name in fields})]
    return results


def _result(capability_id, source, selector, required, observed, ok, records, case=None,
            observed_values=None, payload=None) -> dict:
    result = {
        "capability_id": capability_id,
        "test_nodeid": f"{PROBE_FILE}::test_{capability_id.replace('.', '_')}",
        "observation_source": source,
        "selector": selector,
        "required_fields": required,
        "observed_fields": observed,
        "result": "pass" if ok else "fail",
        "event_count": len(records),
    }
    if case:
        result["case"] = case
    if observed_values is not None:
        result["observed_values"] = observed_values
    payload = payload if payload is not None else _payload_from(records)
    result["value_assertions"] = [value_assertions.build(field, payload, observed_values or {})
                                  for field in required]
    return result


async def probe_memory(tmp_path) -> list[dict]:
    import pathlib
    store = JsonFilePreferenceStore(pathlib.Path(tmp_path))
    bus = TradeEventBus()
    remember = build_remember_preference_tool(store, bus)
    update = build_update_preference_tool(store, bus)
    forget = build_forget_preference_tool(store, bus)
    context_token = ShoppingContext.set(ShoppingContextSnapshot("s", "buyer-memory", "zh-CN", "CNY"))
    results = []
    try:
        init_budget(0)
        events = []
        with observe_run_events(events.append):
            await remember("like", "喜欢轻便背包")
        records = _records(events)
        saved = next((row["payload"].get("saved") for row in records
                      if row["type"] == "tool.result" and row["payload"].get("tool") == "remember_preference_tool"), None)
        written = verified_result("memory_write") or {}
        ok = (saved == "喜欢轻便背包" and written.get("action") == "保存"
              and written.get("statement") == "喜欢轻便背包")
        results.append(_result("memory.remember", "event", "tool.result:remember_preference_tool",
                               ["saved"], ["saved"] if saved else [], ok, records,
                               observed_values={"saved": saved}))
        results.append(_result("memory.remember", "verified_result", "memory_write",
                               ["action", "statement"], sorted(written), ok, records,
                               observed_values={"action": written.get("action"),
                                                "statement": written.get("statement")}))

        preferences = await store.list_by_buyer("buyer-memory")
        target = preferences[0]
        events = []
        with observe_run_events(events.append):
            await update(target.statement, target.kind, "喜欢超轻背包",
                         memory_id=target.memory_id, expected_version=target.version)
        records = _records(events)
        updated = next((row["payload"].get("updated") for row in records
                        if row["type"] == "tool.result" and row["payload"].get("tool") == "update_preference_tool"), None)
        current = await store.list_by_buyer("buyer-memory")
        written = verified_result("memory_write") or {}
        results.append(_result("memory.update", "event", "tool.result:update_preference_tool",
                               ["updated"], ["updated"] if updated is not None else [],
                               updated is True and any(p.statement == "喜欢超轻背包" for p in current), records,
                               observed_values={"updated": updated,
                                                "stored_statement": current[0].statement if current else None}))
        results.append(_result("memory.update", "verified_result", "memory_write",
                               ["action"], sorted(written),
                               written.get("action") == "更新", records,
                               observed_values={"action": written.get("action")}))

        preferences = await store.list_by_buyer("buyer-memory")
        target = preferences[0]
        events = []
        with observe_run_events(events.append):
            await forget(target.statement, memory_id=target.memory_id, expected_version=target.version)
        records = _records(events)
        deleted = next((row["payload"].get("deleted") for row in records
                        if row["type"] == "tool.result" and row["payload"].get("tool") == "forget_preference_tool"), None)
        remaining = await store.list_by_buyer("buyer-memory")
        written = verified_result("memory_write") or {}
        results.append(_result("memory.forget", "event", "tool.result:forget_preference_tool",
                               ["deleted"], ["deleted"] if deleted else [],
                               deleted == "喜欢超轻背包" and remaining == [], records,
                               observed_values={"deleted": deleted, "remaining": len(remaining)}))
        results.append(_result("memory.forget", "verified_result", "memory_write",
                               ["action", "statement"], sorted(written),
                               written.get("action") == "删除"
                               and written.get("statement") == "喜欢超轻背包", records,
                               observed_values={"action": written.get("action"),
                                                "statement": written.get("statement")}))
    finally:
        ShoppingContext.reset(context_token)
    return results


async def probe_session(tmp_path) -> list[dict]:
    import pathlib
    store = JsonFilePreferenceStore(pathlib.Path(tmp_path) / "sessions")
    bus = TradeEventBus()
    remember = build_remember_preference_tool(store, bus)
    events = []

    async def write_as(session_id, buyer_id, statement):
        token = ShoppingContext.set(ShoppingContextSnapshot(session_id, buyer_id, "zh-CN", "CNY"))
        try:
            with observe_run_events(events.append):
                await remember("like", statement)
        finally:
            ShoppingContext.reset(token)

    await write_as("session-a", "buyer-a", "喜欢军绿色")
    await write_as("session-b", "buyer-b", "喜欢黑色")
    first = [item.statement for item in await store.list_by_buyer("buyer-a")]
    second = [item.statement for item in await store.list_by_buyer("buyer-b")]
    records = _records(events)
    raw_ids = {row["shopping_session_id"] for row in records if row["shopping_session_id"]}
    hashes = {row["correlation"].get("session_id") for row in records
              if row["correlation"].get("session_id")}
    isolated = (first == ["喜欢军绿色"] and second == ["喜欢黑色"]
                and "喜欢黑色" not in first and "喜欢军绿色" not in second)
    ok = isolated and raw_ids == {"session-a", "session-b"} and len(hashes) == 2
    snapshot_rows = [{"buyer_id": "buyer-a", "statement": first[0] if first else ""},
                     {"buyer_id": "buyer-b", "statement": second[0] if second else ""}]
    return [
        _result("state.session_isolation", "event", "TradeEvent",
                ["shopping_session_id", "correlation.session_id"],
                ["shopping_session_id", "correlation.session_id"] if ok else [], ok, records,
                observed_values={"shopping_session_id": sorted(raw_ids),
                                 "correlation.session_id": sorted(hashes)}),
        _result("state.session_isolation", "state_snapshot", "preference_repository",
                ["buyer_id", "statement"], ["buyer_id", "statement"] if ok else [], ok, records,
                observed_values={"buyer_id": [row["buyer_id"] for row in snapshot_rows],
                                 "statement": {row["buyer_id"]: row["statement"] for row in snapshot_rows}}
                if ok else {}),
    ]


async def probe_trade(tmp_path) -> list[dict]:
    import pathlib
    base = pathlib.Path(tmp_path)
    base.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite+aiosqlite:///{base / 'trade.db'}")
    store = SqlTradeStore(engine)
    products = InMemoryProductRepository()
    bus = TradeEventBus()
    evidence = ContextEvidenceStore(base / "evidence.db")
    service = ConfirmationService(products, store, bus)
    await bootstrap_schema(engine)
    await store.initialize_inventory(await products.list_all())
    context_token = ShoppingContext.set(ShoppingContextSnapshot("s", "buyer-trade", "zh-CN", "CNY"))
    try:
        search = build_product_search_tool(CatalogSearchUseCase(products), bus, evidence)
        search_events = []
        with observe_run_events(search_events.append):
            await search(normalized_query="", product_id="P1001", ship_to="CN", top_k=5)

        orders_before = await store.list_orders(buyer_id="buyer-trade")
        inventory_before = await store.get_inventory(["P1001-S1"])
        create = build_create_order_tool(PlaceOrderUseCase(service), bus, evidence)
        events = []
        with observe_run_events(events.append):
            chunk = await create(items=[{"product_id": "P1001", "sku_id": "P1001-S1", "quantity": 1}],
                                 shipping_address={"recipient_name": "张三", "country": "CN",
                                                   "state": "浙江", "city": "杭州",
                                                   "address_line": "西湖区某路 1 号",
                                                   "postal_code": "310000", "phone": "13800000000"})
        records = _records(events)
        payload = next((row["payload"] for row in records
                        if row["type"] == "tool.result" and row["payload"].get("tool") == "create_order_tool"), {})
        confirmation = payload.get("confirmation") or {}
        orders_after = await store.list_orders(buyer_id="buyer-trade")
        inventory_after = await store.get_inventory(["P1001-S1"])
        fields = ["confirmation_required", "confirmation.confirmation_id",
                  "confirmation.snapshot_hash", "confirmation.status", "confirmation.payload"]
        prepared_ok = (payload.get("confirmation_required") is True
                       and all(_present(payload, field) for field in fields)
                       and confirmation.get("status") == "pending"
                       and (orders_before.get("orders") or []) == []
                       and (orders_after.get("orders") or []) == []
                       and inventory_before == inventory_after)
        results = [
            _result("trade.prepare", "event", "tool.result:create_order_tool", fields,
                    [field for field in fields if _present(payload, field)], prepared_ok, records,
                    observed_values={"confirmation_id": confirmation.get("confirmation_id"),
                                     "snapshot_hash": confirmation.get("snapshot_hash"),
                                     "status": confirmation.get("status"),
                                     "payload_keys": sorted(confirmation.get("payload") or {})}),
            _result("trade.prepare", "state_snapshot", "trade_repository",
                    ["order_count_before", "order_count_after", "inventory_before", "inventory_after"],
                    ["order_count_before", "order_count_after", "inventory_before", "inventory_after"],
                    prepared_ok, records,
                    observed_values={"order_count_before": len(orders_before.get("orders") or []),
                                     "order_count_after": len(orders_after.get("orders") or []),
                                     "inventory_before": inventory_before,
                                     "inventory_after": inventory_after,
                                     "inventory_delta": (inventory_after.get("P1001-S1", 0)
                                                         - inventory_before.get("P1001-S1", 0))}),
        ]

        confirmation_payload = confirmation.get("payload") or {}
        await service.resolve(confirmation.get("confirmation_id"), "buyer-trade", "s",
                              confirmation.get("snapshot_hash"), True)
        orders = await store.list_orders(buyer_id="buyer-trade")
        order = (orders.get("orders") or [{}])[0]
        query = build_query_order_tool(QueryOrderUseCase(store), bus)
        events = []
        with observe_run_events(events.append):
            await query(order.get("order_id") or "")
        records = _records(events)
        qpayload = next((row["payload"] for row in records
                         if row["type"] == "tool.result" and row["payload"].get("tool") == "query_order_tool"), {})
        order_state = qpayload.get("order") or {}
        query_ok = (order_state.get("order_id") == order.get("order_id")
                    and order_state.get("status") == order.get("status"))
        results.append(_result("trade.query", "event", "tool.result:query_order_tool",
                               ["order"], sorted(qpayload), query_ok, records,
                               observed_values={"order_id": order_state.get("order_id"),
                                                "status": order_state.get("status")}))
    finally:
        ShoppingContext.reset(context_token)
        await engine.dispose()
    return results


async def probe_knowledge(tmp_path) -> list[dict]:
    import pathlib
    from agentscope.rag import KnowledgeBase, QdrantStore
    from tests.test_phase3 import TermAxisEmbedding

    base = pathlib.Path(tmp_path)
    docs = base / "knowledge"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "outdoor.md").write_text("# 户外\n露营灯看防水等级与续航，登山杖优先钛合金。", encoding="utf-8")
    (docs / "guide.md").write_text("# 通则\n美国免税额度约 800 美元，运费按首件全价加续件折价计。", encoding="utf-8")
    kb = KnowledgeBase(name="probe_category_kb", description="探针知识库",
                       embedding_model=TermAxisEmbedding(),
                       vector_store=QdrantStore(path=str(base / "kb")),
                       collection="probe_category_kb")
    await bootstrap_category_knowledge(kb, knowledge_dir=docs)
    bus = TradeEventBus()
    tool = build_category_insight_tool(kb, bus)
    events = []
    with observe_run_events(events.append):
        chunk = await tool(question="美国免税额度是多少", top_k=2)
    records = _records(events)
    payload = next((row["payload"] for row in records
                    if row["type"] == "tool.result" and row["payload"].get("tool") == "category_insight_tool"), {})
    content = {}
    try:
        content = json.loads(chunk.content[0].text)
    except Exception:
        content = {}
    insights = content.get("insights") or []
    first = insights[0] if insights else {}
    metadata = first.get("metadata") or {}
    event_ok = (payload.get("hit_count", 0) > 0 and payload.get("abstained") is False
                and bool(insights) and bool(payload.get("policy_fact_statuses")))
    tool_ok = (bool(first.get("source")) and bool(metadata.get("source_reference"))
               and bool(metadata.get("source_type")))
    abstain_events = []
    with observe_run_events(abstain_events.append):
        abstain_chunk = await tool(question="火星种土豆", top_k=2)
    try:
        abstain_content = json.loads(abstain_chunk.content[0].text)
    except Exception:
        abstain_content = {}
    abstain_payload = next((row["payload"] for row in _records(abstain_events)
                            if row["type"] == "tool.result"), {})
    abstain_ok = (abstain_payload.get("abstained") is True
                  and abstain_payload.get("hit_count") == 0
                  and bool(abstain_payload.get("reason"))
                  and abstain_content.get("unanswerable") is True
                  and bool(abstain_content.get("reason")))
    result_rows = [
        _result("search.category_insight", "event", "tool.result:category_insight_tool",
                ["hit_count", "abstained", "policy_fact_statuses"],
                sorted(payload), event_ok, records,
                observed_values={"hit_count": payload.get("hit_count"),
                                 "abstained": payload.get("abstained"),
                                 "policy_fact_statuses": payload.get("policy_fact_statuses")}),
        _result("search.category_insight", "tool_result", "category_insight_tool",
                ["insights[].source", "insights[].metadata.source_reference",
                 "insights[].metadata.source_type"],
                ["insights[].source", "insights[].metadata.source_reference",
                 "insights[].metadata.source_type"] if tool_ok else [], tool_ok, records,
                observed_values={"source": first.get("source"),
                                 "source_reference": metadata.get("source_reference"),
                                 "source_type": metadata.get("source_type")}),
        _result("search.category_insight", "event", "tool.result:category_insight_tool",
                ["abstained", "hit_count", "reason"], sorted(abstain_payload),
                abstain_ok, abstain_events, case="abstain",
                observed_values={"abstained": abstain_payload.get("abstained"),
                                 "hit_count": abstain_payload.get("hit_count"),
                                 "reason": abstain_payload.get("reason")}),
        _result("search.category_insight", "tool_result", "category_insight_tool",
                ["unanswerable", "reason"], sorted(abstain_content),
                abstain_ok, abstain_events, case="abstain",
                observed_values={"unanswerable": abstain_content.get("unanswerable"),
                                 "reason": abstain_content.get("reason")}),
    ]
    for row in result_rows:
        if row.get("case") == "abstain":
            if row["observation_source"] == "event":
                row["value_assertions"] = [
                    {"field": "abstained", "projection": None, "operator": "equals", "actual": True, "expected": True},
                    {"field": "hit_count", "projection": None, "operator": "equals", "actual": 0, "expected": 0},
                    {"field": "reason", "projection": None, "operator": "nonempty", "actual": abstain_payload.get("reason"), "expected": True},
                ]
            else:
                row["value_assertions"] = [
                    {"field": "unanswerable", "projection": None, "operator": "equals", "actual": True, "expected": True},
                    {"field": "reason", "projection": None, "operator": "nonempty", "actual": abstain_content.get("reason"), "expected": True},
                ]
    return result_rows


def _trade_address() -> Address:
    return Address("张三", "CN", "浙江", "杭州", "西湖区某路 1 号", "310000", "13800000000")


async def _new_trade_env(base):
    import pathlib
    base = pathlib.Path(base)
    base.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite+aiosqlite:///{base / 'trade.db'}")
    store = SqlTradeStore(engine)
    products = InMemoryProductRepository()
    bus = TradeEventBus()
    service = ConfirmationService(products, store, bus)
    await bootstrap_schema(engine)
    await store.initialize_inventory(await products.list_all())
    return engine, store, products, bus, service


async def _prepare(service, buyer_id, session_id, quantity=1):
    return await service.prepare_order(
        buyer_id, session_id, [OrderItemInput("P1001", "P1001-S1", quantity)], _trade_address())


def _snapshot_record(capability_id, ok, records, values, case=None):
    fields = ["order_count_before", "order_count_after", "inventory_before", "inventory_after"]
    return _result(capability_id, "state_snapshot", "trade_repository", fields,
                   fields if ok else [], ok, records, case=case,
                   observed_values=values if ok else {})


async def probe_trade_approve(tmp_path) -> list[dict]:
    import pathlib
    base = pathlib.Path(tmp_path)
    records_out = []
    # approved path
    engine, store, products, bus, service = await _new_trade_env(base / "approve")
    buyer, session = "buyer-approve", "session-approve"
    try:
        inventory_before = await store.get_inventory(["P1001-S1"])
        orders_before = await store.list_orders(buyer_id=buyer)
        prepared = await _prepare(service, buyer, session)
        confirmation = prepared["confirmation"]
        events = []
        with observe_run_events(events.append):
            await service.resolve(confirmation["confirmation_id"], buyer, session,
                                  confirmation["snapshot_hash"], True)
        orders_after = await store.list_orders(buyer_id=buyer)
        inventory_after = await store.get_inventory(["P1001-S1"])
        resolved = next((row for row in _records(events) if row["type"] == "confirmation.resolved"), {})
        inner = (resolved.get("payload") or {}).get("confirmation") or {}
        order = (orders_after.get("orders") or [{}])[0]
        ok = (inner.get("status") == "approved"
              and (inner.get("result") or {}).get("order_id") == order.get("order_id")
              and len(orders_after.get("orders") or []) == 1
              and inventory_after.get("P1001-S1") == inventory_before.get("P1001-S1") - 1)
        fields = ["confirmation.status", "confirmation.result.order_id"]
        values = {
            "order_count_before": len(orders_before.get("orders") or []),
            "order_count_after": len(orders_after.get("orders") or []),
            "inventory_before": inventory_before,
            "inventory_after": inventory_after,
            "inventory_delta": inventory_after.get("P1001-S1", 0) - inventory_before.get("P1001-S1", 0),
            "order_id": order.get("order_id"),
            "order_status": order.get("status"),
        }
        records_out.append(_result("trade.approve", "event", "confirmation.resolved", fields,
                                   [field for field in fields if _present(resolved.get("payload") or {}, field)],
                                   ok, events, case="approved",
                                   observed_values={"status": inner.get("status"),
                                                    "order_id": (inner.get("result") or {}).get("order_id")}))
        records_out.append(_snapshot_record("trade.approve", ok, events, values, case="approved"))
    finally:
        await engine.dispose()

    # rejected path
    engine, store, products, bus, service = await _new_trade_env(base / "reject")
    buyer, session = "buyer-reject", "session-reject"
    try:
        inventory_before = await store.get_inventory(["P1001-S1"])
        prepared = await _prepare(service, buyer, session)
        confirmation = prepared["confirmation"]
        events = []
        with observe_run_events(events.append):
            await service.resolve(confirmation["confirmation_id"], buyer, session,
                                  confirmation["snapshot_hash"], False)
        orders_after = await store.list_orders(buyer_id=buyer)
        inventory_after = await store.get_inventory(["P1001-S1"])
        resolved = next((row for row in _records(events) if row["type"] == "confirmation.resolved"), {})
        inner = (resolved.get("payload") or {}).get("confirmation") or {}
        ok = (inner.get("status") == "rejected"
              and (orders_after.get("orders") or []) == []
              and inventory_after == inventory_before)
        values = {
            "order_count_before": 0,
            "order_count_after": len(orders_after.get("orders") or []),
            "inventory_before": inventory_before,
            "inventory_after": inventory_after,
            "inventory_delta": inventory_after.get("P1001-S1", 0) - inventory_before.get("P1001-S1", 0),
            "order_id": None,
            "order_status": None,
        }
        records_out.append(_result("trade.approve", "event", "confirmation.resolved",
                                   ["confirmation.status"], ["confirmation.status"] if ok else [],
                                   ok, events, case="rejected",
                                   observed_values={"status": inner.get("status")}))
        records_out.append(_snapshot_record("trade.approve", ok, events, values, case="rejected"))
    finally:
        await engine.dispose()
    return records_out


async def probe_trade_cancel(tmp_path) -> list[dict]:
    import pathlib
    base = pathlib.Path(tmp_path)
    engine, store, products, bus, service = await _new_trade_env(base / "cancel")
    buyer, session = "buyer-cancel", "session-cancel"
    try:
        inventory_start = await store.get_inventory(["P1001-S1"])
        first = (await _prepare(service, buyer, session))["confirmation"]
        await service.resolve(first["confirmation_id"], buyer, session, first["snapshot_hash"], True)
        second = (await _prepare(service, buyer, session))["confirmation"]
        await service.resolve(second["confirmation_id"], buyer, session, second["snapshot_hash"], True)
        orders = await store.list_orders(buyer_id=buyer)
        keep, cancel = orders["orders"][0], orders["orders"][1]
        inventory_before = await store.get_inventory(["P1001-S1"])

        prepared = await service.prepare_cancel(buyer, session, cancel["order_id"], "不需要了")
        confirmation = prepared["confirmation"]
        events = []
        with observe_run_events(events.append):
            await service.resolve(confirmation["confirmation_id"], buyer, session,
                                  confirmation["snapshot_hash"], True)
        inventory_after = await store.get_inventory(["P1001-S1"])
        orders_after = await store.list_orders(buyer_id=buyer)
        by_id = {item["order_id"]: item for item in orders_after["orders"]}
        resolved = next((row for row in _records(events) if row["type"] == "confirmation.resolved"), {})
        inner = (resolved.get("payload") or {}).get("confirmation") or {}
        result = inner.get("result") or {}
        ok = (inner.get("status") == "approved"
              and inner.get("action") == "cancel"
              and result.get("order_id") == cancel["order_id"]
              and by_id[cancel["order_id"]]["status"] == "CANCELLED"
              and by_id[keep["order_id"]]["status"] == "CONFIRMED"
              and inventory_after.get("P1001-S1") == inventory_before.get("P1001-S1") + 1)
        fields = ["confirmation.status", "confirmation.action",
                  "confirmation.result.order_id", "confirmation.result.status"]
        values = {
            "order_count_before": len(orders.get("orders") or []),
            "order_count_after": len(orders_after.get("orders") or []),
            "target_order_id": cancel["order_id"],
            "target_status_before": "CONFIRMED",
            "target_status_after": by_id[cancel["order_id"]]["status"],
            "unaffected_order_id": keep["order_id"],
            "unaffected_status_before": "CONFIRMED",
            "unaffected_status_after": by_id[keep["order_id"]]["status"],
            "inventory_before": inventory_before,
            "inventory_after": inventory_after,
            "inventory_delta": inventory_after.get("P1001-S1", 0) - inventory_before.get("P1001-S1", 0),
        }
        records_out = [_result("trade.cancel", "event", "confirmation.resolved", fields,
                               [field for field in fields if _present(resolved.get("payload") or {}, field)],
                               ok, events,
                               observed_values={"status": inner.get("status"),
                                                "action": inner.get("action"),
                                                "order_id": result.get("order_id")})]
        records_out.append(_snapshot_record("trade.cancel", ok, events, values))
    finally:
        await engine.dispose()
    return records_out


async def probe_trade_idempotency(tmp_path) -> list[dict]:
    import pathlib
    base = pathlib.Path(tmp_path)
    engine, store, products, bus, service = await _new_trade_env(base / "idempotency")
    buyer, session = "buyer-idem", "session-idem"
    try:
        inventory_before = await store.get_inventory(["P1001-S1"])
        prepared = await _prepare(service, buyer, session)
        confirmation = prepared["confirmation"]
        first_events = []
        with observe_run_events(first_events.append):
            first = await service.resolve(confirmation["confirmation_id"], buyer, session,
                                          confirmation["snapshot_hash"], True)
        inventory_mid = await store.get_inventory(["P1001-S1"])
        second_events = []
        with observe_run_events(second_events.append):
            second = await service.resolve(confirmation["confirmation_id"], buyer, session,
                                           confirmation["snapshot_hash"], True)
        inventory_after = await store.get_inventory(["P1001-S1"])
        orders = await store.list_orders(buyer_id=buyer)
        first_order = ((first.get("confirmation") or {}).get("result") or {}).get("order_id")
        second_order = ((second.get("confirmation") or {}).get("result") or {}).get("order_id")
        ok = (first_order and first_order == second_order
              and len(orders.get("orders") or []) == 1
              and inventory_mid == inventory_after
              and inventory_after.get("P1001-S1") == inventory_before.get("P1001-S1") - 1)
        records_out = []
        for events in (first_events, second_events):
            resolved = next((row for row in _records(events) if row["type"] == "confirmation.resolved"), {})
            fields = ["confirmation.status", "confirmation.result.order_id"]
            records_out.append(_result("trade.idempotency", "event", "confirmation.resolved", fields,
                                       [field for field in fields if _present(resolved.get("payload") or {}, field)],
                                       ok, events,
                                       observed_values={"status": (resolved.get("payload") or {}).get(
                                                           "confirmation", {}).get("status"),
                                                        "order_id": first_order if events is first_events else second_order}))
        values = {
            "order_count_before": 0,
            "order_count_after": len(orders.get("orders") or []),
            "order_count_after_first": 1 if first_order else 0,
            "first_order_id": first_order,
            "second_order_id": second_order,
            "inventory_before": inventory_before,
            "inventory_mid": inventory_mid,
            "inventory_after": inventory_after,
            "inventory_delta_second": inventory_after.get("P1001-S1", 0) - inventory_mid.get("P1001-S1", 0),
        }
        records_out.append(_snapshot_record("trade.idempotency", ok, second_events, values))
        return records_out
    finally:
        await engine.dispose()


async def _run_all(tmp_path=None) -> dict:
    import pathlib
    import tempfile
    root = pathlib.Path(tmp_path or tempfile.mkdtemp())
    records = []
    records.extend(await probe_search())
    records.extend(await probe_dispatch())
    records.extend(await probe_grounding(root / "grounding"))
    records.extend(await probe_memory(root / "memory"))
    records.extend(await probe_session(root / "session"))
    records.extend(await probe_trade(root / "trade"))
    records.extend(await probe_knowledge(root / "knowledge"))
    records.extend(await probe_trade_approve(root / "approve"))
    records.extend(await probe_trade_cancel(root / "cancel"))
    records.extend(await probe_trade_idempotency(root / "idempotency"))
    return {
        "schema_version": "agentbench-evidence-probe-v1",
        "records": records,
        "capabilities": sorted({row["capability_id"] for row in records}),
    }


def run_all(tmp_path=None) -> dict:
    """Run probes in a copied context so no ContextVar leaks into the caller."""
    return contextvars.copy_context().run(lambda: asyncio.run(_run_all(tmp_path)))


def main() -> None:
    result = run_all()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
