from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json

import pytest

from scripts.eval.run_s01_canary import (
    MODEL, OUTPUT_TOKENS, RECEIPT_SCHEMA,
    backend_environment, local_preflight, proxy_command,
    consume_receipt, validate_receipt,
)


def receipt(source_commit="abc123", **changes):
    row = {
        "schema_version": RECEIPT_SCHEMA,
        "receipt_id": "receipt-s01-one",
        "authorized": True,
        "case_id": "S01",
        "source_commit": source_commit,
        "model": MODEL,
        "temperature": 0,
        "max_output_tokens_per_request": OUTPUT_TOKENS,
        "max_physical_requests": 4,
        "automatic_retries": 0,
        "fallback_model": "",
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    }
    row.update(changes)
    return row


def write_receipt(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def test_receipt_requires_exact_frozen_paid_scope(tmp_path):
    path = tmp_path / "receipt.json"
    write_receipt(path, receipt())
    assert validate_receipt(path, "receipt-s01-one", "abc123")["case_id"] == "S01"
    for field, value in (
        ("model", "deepseek-flash"),
        ("max_output_tokens_per_request", 1024),
        ("max_physical_requests", 5),
        ("automatic_retries", 1),
        ("fallback_model", "qwen-plus"),
    ):
        write_receipt(path, receipt(**{field: value}))
        with pytest.raises(ValueError, match=field):
            validate_receipt(path, "receipt-s01-one", "abc123")


def test_receipt_requires_ack_source_and_future_expiry(tmp_path):
    path = tmp_path / "receipt.json"
    write_receipt(path, receipt())
    with pytest.raises(ValueError, match="acknowledgement"):
        validate_receipt(path, "wrong", "abc123")
    with pytest.raises(ValueError, match="source_commit"):
        validate_receipt(path, "receipt-s01-one", "different")
    write_receipt(path, receipt(expires_at="2020-01-01T00:00:00+00:00"))
    with pytest.raises(ValueError, match="expired"):
        validate_receipt(path, "receipt-s01-one", "abc123")


def test_receipt_rejects_group_access_and_cannot_be_consumed_twice(tmp_path):
    path = tmp_path / "receipt.json"
    write_receipt(path, receipt())
    path.chmod(0o640)
    with pytest.raises(ValueError, match="permissions"):
        validate_receipt(path, "receipt-s01-one", "abc123")
    path.chmod(0o600)
    value = validate_receipt(path, "receipt-s01-one", "abc123")
    output = tmp_path / "evidence"
    consume_receipt(path, value, output)
    with pytest.raises(FileExistsError):
        consume_receipt(path, value, output)


def test_proxy_command_freezes_both_output_arguments(tmp_path):
    command = proxy_command(
        port=18091, upstream="https://provider.example/v1",
        ledger=tmp_path / "ledger.jsonl", limit=4,
    )
    assert command[command.index("--maximum-output") + 1] == "8192"
    assert command[command.index("--expected-output") + 1] == "8192"
    assert command[command.index("--expected-model") + 1] == "deepseek-chat"
    assert command[command.index("--expected-temperature") + 1] == "0"


def test_backend_environment_disables_retry_fallback_and_budget_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9999")
    env = backend_environment(
        proxy_port=18091, backend_port=8005,
        data_dir=tmp_path / "runtime", api_key="test-only-secret",
    )
    assert env["LLM_BASE_URL"] == "http://127.0.0.1:18091/v1"
    assert env["LLM_MODEL"] == "deepseek-chat"
    assert env["LLM_MAX_RETRIES"] == "0"
    assert env["LLM_FALLBACK_MODEL"] == ""
    assert env["TOKEN_BUDGET_TOTAL"] == "0"
    assert env["QUEUE_ENABLED"] == "0"
    assert "ALL_PROXY" not in env


def test_local_preflight_is_mock_only_and_rejects_before_reserve(tmp_path):
    output = tmp_path / "fresh-preflight"
    report = local_preflight(output)
    assert report["network"] == "httpx.MockTransport and local loopback only"
    assert report["rejected_status"] == 400
    assert report["reserved_after_rejection"] == 0
    assert report["accepted_status"] == 200
    assert report["upstream_calls"] == 1
    assert report["forwarded_model"] == "deepseek-chat"
    assert report["forwarded_max_tokens"] == 8192
    assert report["forwarded_temperature"] == 0
    assert report["ledger_errors"] == []
    assert report["passed"] is True
    assert (output / "manifest.json").is_file()
