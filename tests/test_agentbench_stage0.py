# -*- coding: utf-8 -*-
"""Stage 0: ownership, graph scoring, concurrency and isolation contracts."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import jsonschema

from app.application.tools.task_dispatch_tool import build_task_dispatch_tool
from app.application.usecases.confirmation_service import ConfirmationService
from app.application.usecases.order_usecases import OrderItemInput
from app.domain.order.address import Address
from app.infrastructure.agent_execution import (
    agent_execution_scope,
    current_agent_execution,
)
from app.infrastructure.context import ShoppingContext, ShoppingContextSnapshot
from app.infrastructure.eventbus import TradeEventBus
from app.infrastructure.persistence.in_memory_repositories import InMemoryProductRepository
from app.infrastructure.persistence.sql.repositories import bootstrap_schema, create_engine
from app.infrastructure.persistence.sql.trade_store import SqlTradeStore
from app.infrastructure.tracing import current_correlation
from scripts.eval.agentbench.scorer import score_execution_graph, summarize_execution


SEARCH_CONTRACT = {
    "allowed_agents": ["main_agent", "search_agent"],
    "required_capabilities": ["catalog_search"],
    "allowed_edges": [
        ["main_agent", "product_search_tool"],
        ["main_agent", "search_agent"],
        ["search_agent", "product_search_tool"],
    ],
    "forbidden_edges": [["search_agent", "create_order_tool"]],
    "dispatch_policy": {"required": False, "max_dispatches": 2},
    "parallel_groups": [],
}


def test_frozen_spike_tasks_validate_and_are_exact_set():
    root = Path(__file__).resolve().parents[1]
    directory = root / "eval/agent/v1-spike"
    schema = json.loads((directory / "schema.json").read_text())
    tasks = []
    for path in sorted((directory / "tasks").glob("*.json")):
        task = json.loads(path.read_text())
        jsonschema.Draft202012Validator(schema).validate(task)
        tasks.append(task["task_id"])
    assert tasks == ["C-context", "S-search", "T-transaction"]


def event(kind, payload, *, agent="main_agent", dispatch_id=""):
    correlation = {"agent": agent}
    if dispatch_id:
        correlation["dispatch_id"] = dispatch_id
    return {"type": kind, "payload": payload, "correlation": correlation}


def test_execution_scope_uses_existing_correlation_and_restores_on_error():
    assert current_agent_execution().agent == "system"
    try:
        with agent_execution_scope("search_agent", dispatch_id="dispatch-test"):
            correlation = current_correlation()
            assert correlation["agent"] == "search_agent"
            assert correlation["dispatch_id"] == "dispatch-test"
            raise RuntimeError("expected")
    except RuntimeError:
        pass
    assert current_agent_execution().agent == "system"


def test_graph_accepts_direct_and_dispatched_search_and_rejects_missing_owner():
    direct = [event("tool.invoke", {"tool": "product_search_tool"})]
    assert score_execution_graph(direct, SEARCH_CONTRACT)["passed"]

    dispatched = [
        event("agent.dispatch", {"agent": "search_agent", "dispatch_id": "d1"}, dispatch_id="d1"),
        event("tool.invoke", {"tool": "product_search_tool"}, agent="search_agent", dispatch_id="d1"),
        event("tool.result", {"tool": "task_dispatch", "dispatch_id": "d1"}, dispatch_id="d1"),
    ]
    assert score_execution_graph(dispatched, SEARCH_CONTRACT)["passed"]

    missing = json.loads(json.dumps(dispatched))
    del missing[1]["correlation"]["agent"]
    result = score_execution_graph(missing, SEARCH_CONTRACT)
    assert result["status"] == "evidence_invalid"
    assert not result["passed"]


def test_graph_rejects_forbidden_edge():
    trace = [
        event("agent.dispatch", {"agent": "search_agent", "dispatch_id": "d1"}, dispatch_id="d1"),
        event("tool.invoke", {"tool": "create_order_tool"}, agent="search_agent", dispatch_id="d1"),
        event("tool.result", {"tool": "task_dispatch", "dispatch_id": "d1"}, dispatch_id="d1"),
    ]
    result = score_execution_graph(trace, SEARCH_CONTRACT)
    assert not result["passed"]
    assert any(value.startswith("forbidden_edge") for value in result["failures"])


def test_execution_accounting_counts_turns_tools_dispatches_and_unknown_usage():
    trace = [
        event("agent.dispatch", {"agent": "search_agent", "dispatch_id": "d1"}, dispatch_id="d1"),
        event("tool.invoke", {"tool": "product_search_tool"}, agent="search_agent", dispatch_id="d1"),
        {"type": "usage.summary", "payload": {"model_calls": 2, "usage_complete": True}, "correlation": {"agent": "main_agent"}},
        {"type": "usage.summary", "payload": {"model_calls": 1, "usage_complete": True}, "correlation": {"agent": "main_agent"}},
    ]
    assert summarize_execution(trace) == {
        "turns": 2, "physical_model_calls": 3, "usage_complete": True,
        "tool_calls": 1, "dispatches": 1,
    }
    trace[-1]["payload"]["usage_complete"] = False
    assert summarize_execution(trace)["physical_model_calls"] is None


class InterleavedWorker:
    def __init__(self, bus, started, release):
        self.bus, self.started, self.release = bus, started, release

    async def reply(self, _inputs):
        self.started.set()
        await self.release.wait()
        session = ShoppingContext.current_session_id()
        self.bus.publish(session, "tool.invoke", {"tool": "product_search_tool", "args": {}})
        await asyncio.sleep(0)
        self.bus.publish(session, "tool.result", {"tool": "product_search_tool", "hits": []})
        return SimpleNamespace(get_text_content=lambda: "done")


class WorkerFactory:
    def __init__(self, bus, started, release):
        self.bus, self.started, self.release = bus, started, release

    def build(self):
        return InterleavedWorker(self.bus, self.started, self.release)


async def test_parallel_dispatches_keep_distinct_contextvar_ownership():
    bus = TradeEventBus()
    queue = bus.subscribe("session")
    first_started, second_started = asyncio.Event(), asyncio.Event()
    first_release, second_release = asyncio.Event(), asyncio.Event()
    factory_one = WorkerFactory(bus, first_started, first_release)
    factory_two = WorkerFactory(bus, second_started, second_release)
    dispatch_one = build_task_dispatch_tool(factory_one, factory_one, bus, subagent_inject=False)
    dispatch_two = build_task_dispatch_tool(factory_two, factory_two, bus, subagent_inject=False)
    shopping = ShoppingContext.set(ShoppingContextSnapshot("session", "buyer", "zh-CN", "CNY"))
    try:
        with agent_execution_scope("main_agent"):
            task_one = asyncio.create_task(dispatch_one("search_agent", "first"))
            task_two = asyncio.create_task(dispatch_two("search_agent", "second"))
            await asyncio.gather(first_started.wait(), second_started.wait())
            second_release.set()
            await asyncio.sleep(0)
            first_release.set()
            await asyncio.gather(task_one, task_two)
    finally:
        ShoppingContext.reset(shopping)

    events = []
    while not queue.empty():
        events.append((await queue.get()).to_dict())
    invokes = [item for item in events if item["type"] == "tool.invoke"]
    assert len(invokes) == 2
    assert {item["correlation"]["agent"] for item in invokes} == {"search_agent"}
    dispatch_ids = {item["correlation"]["dispatch_id"] for item in invokes}
    assert len(dispatch_ids) == 2
    assert all(value.startswith("dispatch-") for value in dispatch_ids)
    assert current_agent_execution().agent == "system"


async def test_cancelled_scope_restores_parent_and_event_roundtrip_keeps_owner():
    observed = []

    async def child():
        try:
            with agent_execution_scope("search_agent", dispatch_id="cancelled"):
                await asyncio.Event().wait()
        finally:
            observed.append(current_agent_execution().agent)

    task = asyncio.create_task(child())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    assert observed == ["system"]

    bus = TradeEventBus()
    queue = bus.subscribe("roundtrip")
    with agent_execution_scope("trade_agent", dispatch_id="roundtrip-dispatch"):
        bus.publish("roundtrip", "tool.invoke", {"tool": "query_order_tool"})
    original = await queue.get()
    restored = type(original).from_dict(original.to_dict())
    assert restored.correlation["agent"] == "trade_agent"
    assert restored.correlation["dispatch_id"] == "roundtrip-dispatch"


async def isolated_transaction(root: Path):
    root.mkdir()
    engine = create_engine(f"sqlite+aiosqlite:///{root / 'trade.db'}")
    products = InMemoryProductRepository()
    store = SqlTradeStore(engine)
    service = ConfirmationService(products, store, TradeEventBus())
    try:
        await bootstrap_schema(engine)
        await store.initialize_inventory(await products.list_all())
        before = await store.get_inventory(["P1001-S1"])
        prepared = await service.prepare_order(
            "buyer", "session", [OrderItemInput("P1001", "P1001-S1", 1)],
            Address("张三", "CN", "浙江", "杭州", "评测路1号", "310000", "13800000000"),
        )
        confirmation = prepared["confirmation"]
        assert (await store.list_orders(buyer_id="buyer"))["total"] == 0
        approved = await service.resolve(
            confirmation["confirmation_id"], "buyer", "session",
            confirmation["snapshot_hash"], True,
        )
        repeated = await service.resolve(
            confirmation["confirmation_id"], "buyer", "session",
            confirmation["snapshot_hash"], True,
        )
        after = await store.get_inventory(["P1001-S1"])
        return before, after, approved["order"]["order_id"], repeated["order"]["order_id"]
    finally:
        await engine.dispose()


async def test_fresh_per_case_trade_assembly_has_identical_initial_state(tmp_path):
    first = await isolated_transaction(tmp_path / "case-one")
    second = await isolated_transaction(tmp_path / "case-two")
    assert first[0] == second[0] == {"P1001-S1": 50}
    assert first[1] == second[1] == {"P1001-S1": 49}
    assert first[2] == first[3]
    assert second[2] == second[3]
