import json
import gzip
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from scripts.model_budget_proxy import (
    BudgetExhausted, BudgetProxyServer, FreshLedger, LedgerUnavailable,
    enforce_output_limit,
)


class RunningProxy:
    def __init__(self, tmp_path, handler, *, limit=12):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.ledger = FreshLedger(tmp_path / "http-ledger.jsonl", limit)
        self.server = BudgetProxyServer(
            ("127.0.0.1", 0), upstream="https://provider.example/v1",
            ledger=self.ledger, maximum_output=1024, client=client,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/chat/completions"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def test_fresh_ledger_hard_stops_before_limit_plus_one(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = FreshLedger(path, 2)
    ledger.reserve(model="m", requested_output=2048, applied_output=1024)
    ledger.reserve(model="m", requested_output=1024, applied_output=1024)
    with pytest.raises(BudgetExhausted):
        ledger.reserve(model="m", requested_output=1, applied_output=1)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["sequence"] for row in rows if row["event"] == "reserved"] == [1, 2]


def test_concurrent_reservations_never_exceed_limit(tmp_path):
    ledger = FreshLedger(tmp_path / "ledger.jsonl", 12)

    def reserve_once(_):
        try:
            return ledger.reserve(model="m", requested_output=1024, applied_output=1024)[0]
        except BudgetExhausted:
            return None

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(reserve_once, range(40)))
    assert sorted(item for item in results if item is not None) == list(range(1, 13))


def test_ledger_is_fresh_only_and_contains_no_request_body(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = FreshLedger(path, 1)
    _, request_id = ledger.reserve(model="m", requested_output=None, applied_output=1024)
    ledger.finish(request_id, status_code=200, outcome="response")
    with pytest.raises(FileExistsError):
        FreshLedger(path, 1)
    text = path.read_text()
    assert "messages" not in text and "authorization" not in text and "api_key" not in text


@pytest.mark.parametrize(
    ("payload", "requested", "applied"),
    [
        ({"model": "m"}, None, 1024),
        ({"model": "m", "max_tokens": 512}, 512, 512),
        ({"model": "m", "max_completion_tokens": 8192}, 8192, 1024),
        ({"model": "m", "max_tokens": 2048, "max_completion_tokens": 768}, 768, 768),
    ],
)
def test_output_limit_is_enforced(payload, requested, applied):
    normalized, observed, limit = enforce_output_limit(payload, 1024)
    assert observed == requested and limit == applied
    assert normalized["max_tokens"] == applied
    assert "max_completion_tokens" not in normalized


@pytest.mark.parametrize("value", [0, -1, True, "1024"])
def test_invalid_output_limit_is_rejected(value):
    with pytest.raises(ValueError):
        enforce_output_limit({"max_tokens": value}, 1024)


def test_http_handler_rejects_thirteenth_before_upstream(tmp_path):
    upstream = []

    def handler(request):
        upstream.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "ok", "choices": []})

    proxy = RunningProxy(tmp_path, handler)
    try:
        with httpx.Client() as client:
            statuses = [client.post(proxy.url, json={"model": "m", "max_tokens": 4096}).status_code for _ in range(13)]
        assert statuses == [200] * 12 + [429]
        assert len(upstream) == 12
        assert all(row["max_tokens"] == 1024 for row in upstream)
    finally:
        proxy.close()


def test_http_handler_concurrent_limit_is_atomic(tmp_path):
    calls = 0
    lock = threading.Lock()

    def handler(_request):
        nonlocal calls
        with lock:
            calls += 1
        return httpx.Response(200, json={"id": "ok", "choices": []})

    proxy = RunningProxy(tmp_path, handler)
    try:
        def send(_):
            return httpx.post(proxy.url, json={"model": "m"}, timeout=5).status_code
        with ThreadPoolExecutor(max_workers=20) as pool:
            statuses = list(pool.map(send, range(30)))
        assert statuses.count(200) == 12
        assert statuses.count(429) == 18
        assert calls == 12
    finally:
        proxy.close()


def test_compressed_upstream_is_decoded_for_downstream(tmp_path):
    encoded = gzip.compress(b'{"id":"compressed","choices":[]}')

    def handler(_request):
        return httpx.Response(200, content=encoded, headers={"content-encoding": "gzip", "content-type": "application/json"})

    proxy = RunningProxy(tmp_path, handler, limit=1)
    try:
        response = httpx.post(proxy.url, json={"model": "m"})
        assert response.status_code == 200
        assert response.json()["id"] == "compressed"
        assert "content-encoding" not in response.headers
    finally:
        proxy.close()


def test_transport_unknown_halts_batch_before_next_upstream(tmp_path):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("unknown outcome")

    proxy = RunningProxy(tmp_path, handler, limit=12)
    try:
        first = httpx.post(proxy.url, json={"model": "m"})
        assert first.status_code == 502
        second = httpx.post(proxy.url, json={"model": "m"})
        assert second.status_code == 429
        assert calls == 1
        rows = [json.loads(line) for line in proxy.ledger.path.read_text().splitlines()]
        assert rows[-1]["outcome"] == "transport_unknown"
        assert rows[-1]["batch_halted"] is True
    finally:
        proxy.close()


def test_reservation_ledger_failure_stops_before_upstream_and_stays_halted(tmp_path, monkeypatch):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"id": "unexpected"})

    proxy = RunningProxy(tmp_path, handler, limit=12)
    original = proxy.ledger._append
    failures = 0

    def fail_once(row):
        nonlocal failures
        if failures == 0:
            failures += 1
            raise OSError("disk unavailable")
        return original(row)

    monkeypatch.setattr(proxy.ledger, "_append", fail_once)
    try:
        first = httpx.post(proxy.url, json={"model": "m"})
        second = httpx.post(proxy.url, json={"model": "m"})
        assert first.status_code == 503
        assert second.status_code == 429
        assert calls == 0
    finally:
        proxy.close()


def test_finish_ledger_failure_makes_future_reservations_sticky_halted(tmp_path, monkeypatch):
    ledger = FreshLedger(tmp_path / "ledger.jsonl", 3)
    _, request_id = ledger.reserve(model="m", requested_output=1, applied_output=1)
    monkeypatch.setattr(ledger, "_append", lambda _row: (_ for _ in ()).throw(OSError("disk unavailable")))
    with pytest.raises(LedgerUnavailable):
        ledger.finish(request_id, status_code=200, outcome="response")
    with pytest.raises(BudgetExhausted, match="batch halted"):
        ledger.reserve(model="m", requested_output=1, applied_output=1)
