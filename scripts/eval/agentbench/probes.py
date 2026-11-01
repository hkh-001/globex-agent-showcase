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
from app.application.tools.remember_preference_tool import build_remember_preference_tool
from app.application.tools.update_preference_tool import build_update_preference_tool
from app.application.tools.forget_preference_tool import build_forget_preference_tool
from app.infrastructure.budget import verified_result
from app.application.usecases.order_usecases import PlaceOrderUseCase, QueryOrderUseCase
from app.application.usecases.confirmation_service import ConfirmationService
from app.application.tools.order_tools import build_create_order_tool, build_query_order_tool
from app.infrastructure.persistence.sql.repositories import bootstrap_schema, create_engine
from app.infrastructure.persistence.sql.trade_store import SqlTradeStore
from app.infrastructure.persistence.context_evidence import ContextEvidenceStore

PROBE_FILE = "eval/agent/v1/probes/test_evidence_probe.py"


class _EvidenceStore:
    async def save(self, *_args, **_kwargs):
        return "probe-ref"


def _records(events) -> list[dict]:
    return [{"type": event.type, "payload": event.payload, "correlation": getattr(event, "correlation", {})}
            for event in events]


def check_fields(records: list[dict], selector: str, fields: list[str]) -> tuple[bool, list[str]]:
    """Return whether the selector event exists and every listed field is present."""
    kind, _, name = selector.partition(":")
    matching = [row for row in records if row["type"] == kind and (
        not name or str((row["payload"] or {}).get("tool") or "") == name)]
    if not matching:
        return False, []
    payload = matching[-1]["payload"] or {}
    observed = sorted(payload)
    missing = [field for field in fields if field not in payload]
    return not missing, observed


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
    results.append(_result("search.catalog", "event", "tool.result:product_search_tool",
                           ["tool", "hit_count", "recall_strategy", "total_candidates",
                            "query_conditions", "hits", "filtered_out", "result_ref"], observed, ok, catalog))
    constraints_ok, constraints_observed = check_fields(
        catalog, "tool.result:product_search_tool",
        ["query_conditions", "filtered_out"])
    results.append(_result("search.constraints", "event", "tool.result:product_search_tool",
                           ["query_conditions", "filtered_out"], constraints_observed,
                           constraints_ok, catalog))

    payload = next((row["payload"] for row in catalog if row["type"] == "tool.result"), {})
    hit = (payload.get("hits") or [{}])[0]
    landed = hit.get("landed_price") or {}
    landed_fields = ["subtotal_major", "freight_major", "tariff_major", "landed_total_major", "currency"]
    results.append(_result("search.landed_price", "event", "tool.result:product_search_tool",
                           [f"hits[].landed_price.{name}" for name in landed_fields],
                           sorted(landed), all(name in landed for name in landed_fields), catalog))

    exact = await run(normalized_query="", product_id="P1003", ship_to="CN", top_k=5)
    exact_payload = next((row["payload"] for row in exact if row["type"] == "tool.result"), {})
    exact_hits = exact_payload.get("hits") or []
    ok_exact = exact_payload.get("recall_strategy") == "exact_id_lookup" and any(
        item.get("product_id") == "P1003" for item in exact_hits)
    results.append(_result("search.exact_id", "event", "tool.result:product_search_tool",
                           ["recall_strategy", "hits", "query_conditions"], sorted(exact_payload),
                           ok_exact, exact))

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
    results.append(_result("state.constraint_carry", "event", "tool.result:product_search_tool",
                           ["constraint_continuity.inherited", "query_conditions.price_max_major",
                            "query_conditions.ship_to", "query_conditions.sku_spec"],
                           sorted(carried_payload),
                           bool(inherited), carried))
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
    results = [_result("dispatch.search", "event", "tool.result:task_dispatch",
                       ["tool", "agent", "dispatch_id", "status"], observed, ok, records)]
    verified_ok = isinstance(verified, list) and "P1003" in verified
    results.append(_result("dispatch.search", "tool_result", "task_dispatch",
                           ["verified_product_ids", "unverified_product_ids"],
                           ["verified_product_ids", "unverified_product_ids"] if verified is not None else [],
                           verified_ok, records))
    decision = {}
    try:
        decision = json.loads(content)
    except Exception:
        decision = {}
    results[-1]["observed_values"] = {"status": decision.get("status"),
                                      "verified_product_ids": decision.get("verified_product_ids"),
                                      "unverified_product_ids": decision.get("unverified_product_ids")}
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
    results.append(_result("dispatch.trade", "event", "tool.result:task_dispatch",
                           ["tool", "agent", "dispatch_id", "status"], trade_observed,
                           trade_ok, trade_records))
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
                           trade_ok_result, trade_records))
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
    results = [_result("harness.grounding_gate", "event", "error", ["message", "claim_guard"],
                       observed, ok_event, records),
               _result("harness.grounding_gate", "private_artifact", "grounding_audit.jsonl",
                       fields, sorted(row), all(name in row for name in fields), records)]
    return results


def _result(capability_id, source, selector, required, observed, ok, records) -> dict:
    return {
        "capability_id": capability_id,
        "test_nodeid": f"{PROBE_FILE}::test_{capability_id.replace('.', '_')}",
        "observation_source": source,
        "selector": selector,
        "required_fields": required,
        "observed_fields": observed,
        "result": "pass" if ok else "fail",
        "event_count": len(records),
    }


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
        ok = saved == "喜欢轻便背包" and written.get("action") == "保存"
        results.append(_result("memory.remember", "event", "tool.result:remember_preference_tool",
                               ["saved"], ["saved"] if saved else [], ok, records))
        results.append(_result("memory.remember", "verified_result", "memory_write",
                               ["action", "statement"], sorted(written), ok, records))

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
                               updated is True and any(p.statement == "喜欢超轻背包" for p in current), records))
        results.append(_result("memory.update", "verified_result", "memory_write",
                               ["action", "statement"], sorted(written),
                               written.get("action") == "更新", records))

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
                               deleted == "喜欢超轻背包" and remaining == [], records))
        results.append(_result("memory.forget", "verified_result", "memory_write",
                               ["action", "statement"], sorted(written),
                               written.get("action") == "删除", records))
    finally:
        ShoppingContext.reset(context_token)
    return results


async def probe_session(tmp_path) -> list[dict]:
    import pathlib
    store = JsonFilePreferenceStore(pathlib.Path(tmp_path) / "sessions")
    bus = TradeEventBus()
    remember = build_remember_preference_tool(store, bus)
    first_token = ShoppingContext.set(ShoppingContextSnapshot("session-a", "buyer-a", "zh-CN", "CNY"))
    try:
        events_a = []
        with observe_run_events(events_a.append):
            await remember("like", "喜欢军绿色")
    finally:
        ShoppingContext.reset(first_token)
    second_token = ShoppingContext.set(ShoppingContextSnapshot("session-b", "buyer-b", "zh-CN", "CNY"))
    try:
        second_preferences = await store.list_by_buyer("buyer-b")
    finally:
        ShoppingContext.reset(second_token)
    records = _records(events_a)
    session_hashes = {row["correlation"].get("session_id") for row in records if row["correlation"].get("session_id")}
    first_preferences = await store.list_by_buyer("buyer-a")
    ok = (len(first_preferences) == 1 and second_preferences == [] and len(session_hashes) == 1)
    snapshot_rows = [{"buyer_id": "buyer-a", "statement": "喜欢军绿色"}] if first_preferences else []
    return [
        _result("state.session_isolation", "event", "TradeEvent",
                ["shopping_session_id", "correlation.session_id"],
                ["correlation.session_id"] if session_hashes else [], ok, records),
        _result("state.session_isolation", "state_snapshot", "preference_repository",
                ["buyer_id", "statement"], sorted(snapshot_rows[0]) if snapshot_rows else [],
                ok and bool(snapshot_rows), records),
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
        prepared_ok = (payload.get("confirmation_required") is True
                       and bool(confirmation.get("confirmation_id"))
                       and bool(confirmation.get("snapshot_hash"))
                       and confirmation.get("status") in (None, "pending"))
        results = [_result("trade.prepare", "event", "tool.result:create_order_tool",
                           ["confirmation_required", "confirmation"], sorted(payload),
                           prepared_ok, records)]

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
                               ["order"], sorted(qpayload), query_ok, records))
    finally:
        ShoppingContext.reset(context_token)
        await engine.dispose()
    return results


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
