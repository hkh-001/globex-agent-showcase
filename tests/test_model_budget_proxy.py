import json
import gzip
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

from scripts.model_budget_proxy import (
    BudgetExhausted, BudgetProxyServer, FreshLedger, LedgerUnavailable,
    enforce_output_limit,
    ResponseMetadataObserver, validate_successful_ledger,
)


class RunningProxy:
    def __init__(self, tmp_path, handler, *, limit=12, expected_model="m", expected_output=None):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        self.ledger = FreshLedger(tmp_path / "http-ledger.jsonl", limit)
        self.server = BudgetProxyServer(
            ("127.0.0.1", 0), upstream="https://provider.example/v1",
            ledger=self.ledger, maximum_output=1024,
            expected_model=expected_model, expected_output=expected_output, client=client,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/chat/completions"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def complete_response(response_id="ok", *, model="m", finish_reason="stop"):
    return {
        "id": response_id,
        "model": model,
        "choices": [{"message": {"content": "ok"}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


def test_fresh_ledger_hard_stops_before_limit_plus_one(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = FreshLedger(path, 2)
    ledger.reserve(requested_model="m", requested_output=2048, applied_output=1024)
    ledger.reserve(requested_model="m", requested_output=1024, applied_output=1024)
    with pytest.raises(BudgetExhausted):
        ledger.reserve(requested_model="m", requested_output=1, applied_output=1)
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [row["sequence"] for row in rows if row["event"] == "reserved"] == [1, 2]


def test_concurrent_reservations_never_exceed_limit(tmp_path):
    ledger = FreshLedger(tmp_path / "ledger.jsonl", 12)

    def reserve_once(_):
        try:
            return ledger.reserve(requested_model="m", requested_output=1024, applied_output=1024)[0]
        except BudgetExhausted:
            return None

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(reserve_once, range(40)))
    assert sorted(item for item in results if item is not None) == list(range(1, 13))


def test_ledger_is_fresh_only_and_contains_no_request_body(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = FreshLedger(path, 1)
    _, request_id = ledger.reserve(requested_model="m", requested_output=None, applied_output=1024)
    ledger.finish(request_id, status_code=200, outcome="response")
    with pytest.raises(FileExistsError):
        FreshLedger(path, 1)
    text = path.read_text()
    assert "messages" not in text and "authorization" not in text and "api_key" not in text


def test_response_metadata_observer_reads_json_without_content():
    observer = ResponseMetadataObserver("application/json")
    observer.feed(json.dumps({
        "model": "served-model",
        "choices": [{"message": {"content": "private answer"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }).encode())
    metadata = observer.finish()
    assert metadata == {
        "content_type": "application/json",
        "events_seen": 1,
        "served_model": "served-model",
        "finish_reason": "stop",
        "usage": {"completion_tokens": 7, "prompt_tokens": 11, "total_tokens": 18},
        "metadata_complete": True,
    }
    assert "private answer" not in json.dumps(metadata)


def test_response_metadata_observer_reads_split_sse_and_usage():
    observer = ResponseMetadataObserver("text/event-stream; charset=utf-8")
    stream = (
        'data: {"model":"served-model","choices":[{"delta":{"content":"secret"},"finish_reason":null}]}\n\n'
        'data: {"model":"served-model","choices":[{"delta":{},"finish_reason":"length"}],'
        '"usage":{"prompt_tokens":20,"completion_tokens":1024,"total_tokens":1044}}\n\n'
        'data: [DONE]\n\n'
    )
    payload = stream.encode()
    for start in range(0, len(payload), 17):
        observer.feed(payload[start:start + 17])
    metadata = observer.finish()
    assert metadata["finish_reason"] == "length"
    assert metadata["served_model"] == "served-model"
    assert metadata["usage"]["completion_tokens"] == 1024
    assert metadata["metadata_complete"] is True
    assert "secret" not in json.dumps(metadata)


def test_response_metadata_observer_handles_split_multibyte_utf8():
    observer = ResponseMetadataObserver("text/event-stream")
    payload = (
        'data: {"model":"served-model","choices":[{"delta":{"content":"中"},'
        '"finish_reason":"stop"}],"usage":{"completion_tokens":1,"total_tokens":3}}\n\n'
    ).encode()
    for byte in payload:
        observer.feed(bytes([byte]))
    metadata = observer.finish()
    assert metadata["served_model"] == "served-model"
    assert metadata["finish_reason"] == "stop"
    assert metadata["metadata_complete"] is True


def test_response_metadata_observer_bounds_malformed_stream_without_leaking_content():
    observer = ResponseMetadataObserver("text/event-stream")
    observer.feed(b"data: " + b"private" * 400_000)
    metadata = observer.finish()
    assert metadata["metadata_complete"] is False
    assert metadata["events_seen"] == 0
    assert "private" not in json.dumps(metadata)


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
        return httpx.Response(200, json=complete_response())

    proxy = RunningProxy(tmp_path, handler)
    try:
        with httpx.Client() as client:
            statuses = [client.post(proxy.url, json={"model": "m", "max_tokens": 4096}).status_code for _ in range(13)]
        assert statuses == [200] * 12 + [429]
        assert len(upstream) == 12
        assert all(row["max_tokens"] == 1024 for row in upstream)
    finally:
        proxy.close()


@pytest.mark.parametrize("bad_model", [None, True, {"private_prompt": "LEAK-ME"}, ["m"], "", "a b", "a\ncontrol", "x" * 201])
def test_http_handler_rejects_unsafe_model_before_reserve_or_upstream(tmp_path, bad_model):
    upstream = []

    def handler(request):
        upstream.append(request)
        return httpx.Response(200, json=complete_response())

    proxy = RunningProxy(tmp_path, handler, limit=1)
    try:
        response = httpx.post(proxy.url, json={
            "model": bad_model,
            "messages": [{"role": "user", "content": "private prompt"}],
        })
        assert response.status_code == 400
        assert upstream == []
        ledger_text = proxy.ledger.path.read_text()
        assert '"event": "reserved"' not in ledger_text
        assert "LEAK-ME" not in ledger_text
        assert "private prompt" not in ledger_text
    finally:
        proxy.close()


@pytest.mark.parametrize("requested_output", [None, 100, 1023])
def test_http_handler_rejects_non_frozen_output_before_reserve_or_upstream(tmp_path, requested_output):
    upstream = []

    def handler(request):
        upstream.append(request)
        return httpx.Response(200, json=complete_response())

    proxy = RunningProxy(tmp_path, handler, limit=1, expected_output=1024)
    try:
        payload = {"model": "m"}
        if requested_output is not None:
            payload["max_tokens"] = requested_output
        response = httpx.post(proxy.url, json=payload)
        assert response.status_code == 400
        assert upstream == []
        assert '"event": "reserved"' not in proxy.ledger.path.read_text()
    finally:
        proxy.close()


def test_http_handler_accepts_exact_frozen_output(tmp_path):
    upstream = []

    def handler(request):
        upstream.append(json.loads(request.content))
        return httpx.Response(200, json=complete_response())

    proxy = RunningProxy(tmp_path, handler, limit=1, expected_output=1024)
    try:
        response = httpx.post(proxy.url, json={"model": "m", "max_tokens": 1024})
        assert response.status_code == 200
        assert upstream[0]["max_tokens"] == 1024
    finally:
        proxy.close()


def test_http_handler_concurrent_limit_is_atomic(tmp_path):
    calls = 0
    lock = threading.Lock()

    def handler(_request):
        nonlocal calls
        with lock:
            calls += 1
        return httpx.Response(200, json=complete_response())

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


def test_incomplete_success_metadata_halts_before_next_upstream(tmp_path):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"id": "missing-metadata", "choices": []})

    proxy = RunningProxy(tmp_path, handler, limit=3)
    try:
        first = httpx.post(proxy.url, json={"model": "m"})
        second = httpx.post(proxy.url, json={"model": "m"})
        assert first.status_code == 200
        assert second.status_code == 429
        assert calls == 1
        rows = [json.loads(line) for line in proxy.ledger.path.read_text().splitlines()]
        assert rows[-1]["outcome"] == "response_metadata_incomplete"
        assert rows[-1]["batch_halted"] is True
        assert rows[-1]["response_metadata"]["metadata_complete"] is False
    finally:
        proxy.close()


def test_served_model_mismatch_halts_before_next_upstream(tmp_path):
    calls = 0

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=complete_response(model="unexpected-model"))

    proxy = RunningProxy(tmp_path, handler, limit=3, expected_model="requested-model")
    try:
        first = httpx.post(proxy.url, json={"model": "requested-model"})
        second = httpx.post(proxy.url, json={"model": "requested-model"})
        assert first.status_code == 200
        assert second.status_code == 429
        assert calls == 1
        rows = [json.loads(line) for line in proxy.ledger.path.read_text().splitlines()]
        assert rows[-1]["outcome"] == "served_model_mismatch"
        assert rows[-1]["batch_halted"] is True
    finally:
        proxy.close()


def test_oversized_metadata_observation_does_not_truncate_response_and_halts_batch(tmp_path):
    calls = 0
    body = json.dumps({
        **complete_response(),
        "padding": "x" * (2 * 1024 * 1024),
    }).encode()

    def handler(_request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    proxy = RunningProxy(tmp_path, handler, limit=3)
    try:
        first = httpx.post(proxy.url, json={"model": "m"}, timeout=10)
        second = httpx.post(proxy.url, json={"model": "m"})
        assert first.status_code == 200
        assert first.content == body
        assert second.status_code == 429
        assert calls == 1
        rows = [json.loads(line) for line in proxy.ledger.path.read_text().splitlines()]
        assert rows[-1]["outcome"] == "response_metadata_incomplete"
        assert rows[-1]["response_metadata"]["metadata_complete"] is False
        assert "padding" not in proxy.ledger.path.read_text()
    finally:
        proxy.close()


def test_concurrent_incomplete_response_serially_halts_second_request(tmp_path):
    calls = 0
    lock = threading.Lock()

    def handler(_request):
        nonlocal calls
        with lock:
            calls += 1
        return httpx.Response(200, json={"id": "missing-metadata", "choices": []})

    proxy = RunningProxy(tmp_path, handler, limit=3)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: httpx.post(proxy.url, json={"model": "m"}).status_code, range(2)))
        assert sorted(statuses) == [200, 429]
        assert calls == 1
    finally:
        proxy.close()


def test_compressed_upstream_is_decoded_for_downstream(tmp_path):
    encoded = gzip.compress(json.dumps(complete_response("compressed")).encode())

    def handler(_request):
        return httpx.Response(200, content=encoded, headers={"content-encoding": "gzip", "content-type": "application/json"})

    proxy = RunningProxy(tmp_path, handler, limit=1)
    try:
        response = httpx.post(proxy.url, json={"model": "m"})
        assert response.status_code == 200
        assert response.json()["id"] == "compressed"
        assert "content-encoding" not in response.headers
        rows = [json.loads(line) for line in proxy.ledger.path.read_text().splitlines()]
        assert rows[-1]["response_metadata"]["content_type"] == "application/json"
    finally:
        proxy.close()


def test_http_ledger_records_cap_temperature_and_response_metadata_only(tmp_path):
    def handler(_request):
        return httpx.Response(200, json={
            "model": "served-model",
            "choices": [{"message": {"content": "private answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
        })

    proxy = RunningProxy(tmp_path, handler, limit=1, expected_model="requested-model")
    try:
        response = httpx.post(proxy.url, json={
            "model": "requested-model", "temperature": 0,
            "max_completion_tokens": 8192,
            "messages": [{"role": "user", "content": "private prompt"}],
        })
        assert response.status_code == 200
        rows = [json.loads(line) for line in proxy.ledger.path.read_text().splitlines()]
        reserved = next(row for row in rows if row["event"] == "reserved")
        finished = next(row for row in rows if row["event"] == "finished")
        assert reserved["sequence"] == 1
        assert reserved["requested_model"] == "requested-model"
        assert "model" not in reserved
        assert reserved["requested_output"] == 8192
        assert reserved["applied_output"] == 1024
        assert reserved["temperature"] == 0
        assert finished["response_metadata"]["served_model"] == "served-model"
        assert finished["response_metadata"]["finish_reason"] == "stop"
        assert finished["response_metadata"]["usage"]["total_tokens"] == 6
        ledger_text = proxy.ledger.path.read_text()
        assert "private prompt" not in ledger_text
        assert "private answer" not in ledger_text
        assert "messages" not in ledger_text
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
    _, request_id = ledger.reserve(requested_model="m", requested_output=1, applied_output=1)
    monkeypatch.setattr(ledger, "_append", lambda _row: (_ for _ in ()).throw(OSError("disk unavailable")))
    with pytest.raises(LedgerUnavailable):
        ledger.finish(request_id, status_code=200, outcome="response")
    with pytest.raises(BudgetExhausted, match="batch halted"):
        ledger.reserve(requested_model="m", requested_output=1, applied_output=1)


def test_successful_ledger_validator_binds_request_and_response_identity(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = FreshLedger(path, 1)
    _, request_id = ledger.reserve(
        requested_model="deepseek-chat", requested_output=8192,
        applied_output=8192, temperature=0,
    )
    ledger.finish(request_id, status_code=200, outcome="response", response_metadata={
        "content_type": "application/json", "events_seen": 1,
        "served_model": "deepseek-chat", "finish_reason": "stop",
        "usage": {"completion_tokens": 12, "total_tokens": 30},
        "metadata_complete": True,
    })
    assert validate_successful_ledger(
        path, expected_model="deepseek-chat", expected_output=8192,
        expected_temperature=0,
    ) == []
    assert "sequence_1:served_model_mismatch" in validate_successful_ledger(
        path, expected_model="deepseek-chat", expected_output=8192,
        allowed_served_models={"provider-alias"},
    )


def test_ledger_validator_rejects_missing_finish_and_wrong_cap(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = FreshLedger(path, 1)
    ledger.reserve(
        requested_model="deepseek-chat", requested_output=8192,
        applied_output=1024, temperature=0,
    )
    errors = validate_successful_ledger(
        path, expected_model="deepseek-chat", expected_output=8192,
    )
    assert "sequence_1:finished_count_0" in errors


def test_ledger_validator_rejects_empty_ledger(tmp_path):
    path = tmp_path / "ledger.jsonl"
    FreshLedger(path, 1)
    errors = validate_successful_ledger(
        path, expected_model="deepseek-chat", expected_output=8192,
        expected_temperature=0,
    )
    assert "insufficient_reserved_requests" in errors


def test_ledger_validator_rejects_requested_cap_mismatch(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = FreshLedger(path, 1)
    _, request_id = ledger.reserve(
        requested_model="deepseek-chat", requested_output=100,
        applied_output=8192, temperature=0,
    )
    ledger.finish(request_id, status_code=200, outcome="response", response_metadata={
        "content_type": "application/json", "events_seen": 1,
        "served_model": "deepseek-chat", "finish_reason": "stop",
        "usage": {"completion_tokens": 12, "total_tokens": 30},
        "metadata_complete": True,
    })
    errors = validate_successful_ledger(
        path, expected_model="deepseek-chat", expected_output=8192,
        expected_temperature=0,
    )
    assert "sequence_1:requested_output_mismatch" in errors
