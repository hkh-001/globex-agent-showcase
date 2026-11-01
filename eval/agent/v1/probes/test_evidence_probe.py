"""Runtime evidence probes for the AgentBench capability matrix.

Each test exercises the real code path through `scripts.eval.agentbench.probes`
and fails when any required observation is missing.  No model or network access.
"""
from __future__ import annotations

import pytest

from scripts.eval.agentbench import probes


@pytest.fixture(scope="module")
def probe_records():
    return probes.run_all()["records"]


def _assert_capability(records, capability_id):
    rows = [row for row in records if row["capability_id"] == capability_id]
    assert rows, f"no probe record for {capability_id}"
    failures = [row for row in rows if row["result"] != "pass"]
    assert not failures, failures


def test_search_catalog(probe_records):
    _assert_capability(probe_records, "search.catalog")


def test_search_constraints(probe_records):
    _assert_capability(probe_records, "search.constraints")


def test_search_landed_price(probe_records):
    _assert_capability(probe_records, "search.landed_price")


def test_search_exact_id(probe_records):
    _assert_capability(probe_records, "search.exact_id")


def test_state_constraint_carry(probe_records):
    _assert_capability(probe_records, "state.constraint_carry")


def test_dispatch_search(probe_records):
    _assert_capability(probe_records, "dispatch.search")


def test_dispatch_trade(probe_records):
    _assert_capability(probe_records, "dispatch.trade")


def test_harness_grounding_gate(probe_records):
    _assert_capability(probe_records, "harness.grounding_gate")


def test_memory_remember(probe_records):
    _assert_capability(probe_records, "memory.remember")


def test_memory_update(probe_records):
    _assert_capability(probe_records, "memory.update")


def test_memory_forget(probe_records):
    _assert_capability(probe_records, "memory.forget")


def test_state_session_isolation(probe_records):
    _assert_capability(probe_records, "state.session_isolation")


def test_trade_prepare(probe_records):
    _assert_capability(probe_records, "trade.prepare")


def test_trade_query(probe_records):
    _assert_capability(probe_records, "trade.query")


def test_search_category_insight(probe_records):
    _assert_capability(probe_records, "search.category_insight")
