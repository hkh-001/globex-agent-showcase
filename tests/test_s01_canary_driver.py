from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import httpx
import pytest

from scripts.eval.run_s01_canary import (
    MODEL, OUTPUT_TOKENS, RECEIPT_SCHEMA,
    backend_environment, local_preflight, proxy_command,
    consume_receipt, event_contract, probe_local_embedding,
    run_paid, validate_local_embedding, validate_receipt,
)
import scripts.eval.run_s01_canary as driver


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


@pytest.fixture
def authorization_root(tmp_path, monkeypatch):
    root = tmp_path / "authorizations"
    root.mkdir()
    monkeypatch.setattr(driver, "AUTHORIZATION_ROOT", root)
    monkeypatch.setattr(driver, "CONSUMED_ROOT", root / "consumed")
    original_run = driver.subprocess.run

    def run(command, *args, **kwargs):
        if command[:2] == ["git", "check-ignore"]:
            return SimpleNamespace(returncode=0)
        return original_run(command, *args, **kwargs)

    monkeypatch.setattr(driver.subprocess, "run", run)
    return root


def test_receipt_requires_exact_frozen_paid_scope(authorization_root):
    path = authorization_root / "receipt.json"
    write_receipt(path, receipt())
    assert validate_receipt(path, "receipt-s01-one", "abc123")["case_id"] == "S01"
    for field, value in (
        ("model", "deepseek-chat"),
        ("max_output_tokens_per_request", 1024),
        ("max_physical_requests", 5),
        ("automatic_retries", 1),
        ("fallback_model", "qwen-plus"),
    ):
        write_receipt(path, receipt(**{field: value}))
        with pytest.raises(ValueError, match=field):
            validate_receipt(path, "receipt-s01-one", "abc123")


def test_receipt_requires_ack_source_and_future_expiry(authorization_root):
    path = authorization_root / "receipt.json"
    write_receipt(path, receipt())
    with pytest.raises(ValueError, match="acknowledgement"):
        validate_receipt(path, "wrong", "abc123")
    with pytest.raises(ValueError, match="source_commit"):
        validate_receipt(path, "receipt-s01-one", "different")
    write_receipt(path, receipt(expires_at="2020-01-01T00:00:00+00:00"))
    with pytest.raises(ValueError, match="expired"):
        validate_receipt(path, "receipt-s01-one", "abc123")


def test_receipt_rejects_group_access_and_cannot_be_consumed_twice(authorization_root, tmp_path):
    path = authorization_root / "receipt.json"
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


def test_receipt_rejects_symlink_and_copy_with_same_id(authorization_root, tmp_path):
    path = authorization_root / "receipt.json"
    write_receipt(path, receipt())
    link = authorization_root / "linked.json"
    link.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        validate_receipt(link, "receipt-s01-one", "abc123")
    value = validate_receipt(path, "receipt-s01-one", "abc123")
    outside = tmp_path / "outside-consumption"
    outside.mkdir()
    symlinked_consumed = authorization_root / "symlinked-consumed"
    symlinked_consumed.symlink_to(outside, target_is_directory=True)
    driver.CONSUMED_ROOT = symlinked_consumed
    with pytest.raises(ValueError, match="directories must not be symlinks"):
        consume_receipt(path, value, tmp_path / "blocked")
    driver.CONSUMED_ROOT = authorization_root / "consumed"
    consume_receipt(path, value, tmp_path / "first")
    copied = authorization_root / "copied.json"
    write_receipt(copied, receipt())
    copied_value = validate_receipt(copied, "receipt-s01-one", "abc123")
    with pytest.raises(FileExistsError):
        consume_receipt(copied, copied_value, tmp_path / "second")


def test_proxy_command_freezes_both_output_arguments(tmp_path):
    command = proxy_command(
        port=18091, upstream="https://provider.example/v1",
        ledger=tmp_path / "ledger.jsonl", limit=4,
    )
    assert command[command.index("--maximum-output") + 1] == "8192"
    assert command[command.index("--expected-output") + 1] == "8192"
    assert command[command.index("--expected-model") + 1] == "deepseek-flash"
    assert command[command.index("--expected-temperature") + 1] == "0"


def test_backend_environment_disables_retry_fallback_and_budget_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:9999")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "must-not-survive")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "https://telemetry.example")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=secret")
    env = backend_environment(
        proxy_port=18091, backend_port=8005,
        data_dir=tmp_path / "runtime", api_key="test-only-secret",
        embedding={"base_url": "http://127.0.0.1:11434/v1", "model": "bge-m3",
                   "version": "ollama-bge-m3", "dimension": 1024},
    )
    assert env["LLM_BASE_URL"] == "http://127.0.0.1:18091/v1"
    assert env["LLM_MODEL"] == "deepseek-flash"
    assert env["LLM_MAX_RETRIES"] == "0"
    assert env["LLM_FALLBACK_MODEL"] == ""
    assert env["TOKEN_BUDGET_TOTAL"] == "0"
    assert env["QUEUE_ENABLED"] == "0"
    assert env["RERANKER_MODE"] == "disabled"
    assert env["TAVILY_API_KEY"] == ""
    assert env["MODEL_RECOVERY_MODE"] == "off"
    assert env["LLM_ENABLE_THINKING"] == "0"
    assert env["EMBEDDING_BASE_URL"] == "http://127.0.0.1:11434/v1"
    assert "ALL_PROXY" not in env
    assert env["OTEL_SDK_DISABLED"] == "true"
    assert env["LANGFUSE_SECRET_KEY"] == ""
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == ""

    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "LANGFUSE_SECRET_KEY=revived-secret\n"
        "OTEL_EXPORTER_OTLP_ENDPOINT=https://telemetry.example\n"
    )
    probe = subprocess.run(
        [sys.executable, "-c",
         "from dotenv import load_dotenv; import os,sys; load_dotenv(sys.argv[1]); "
         "print(repr((os.getenv('LANGFUSE_SECRET_KEY'), os.getenv('OTEL_EXPORTER_OTLP_ENDPOINT'))))",
         str(dotenv)],
        env=env, check=True, capture_output=True, text=True,
    )
    assert probe.stdout.strip() == "('', '')"


def test_local_embedding_must_be_loopback_bge_m3_1024():
    valid = SimpleNamespace(
        embedding_base_url="http://127.0.0.1:11434/v1",
        embedding_model="bge-m3", embedding_version="ollama", embedding_dim=1024,
    )
    assert validate_local_embedding(valid)["dimension"] == 1024
    for change in (
        {"embedding_base_url": "https://provider.example/v1"},
        {"embedding_model": "remote-model"},
        {"embedding_dim": 512},
    ):
        values = vars(valid) | change
        with pytest.raises(ValueError):
            validate_local_embedding(SimpleNamespace(**values))


def test_local_embedding_probe_requires_expected_dimension():
    embedding = {
        "base_url": "http://127.0.0.1:11434/v1", "model": "bge-m3",
        "version": "ollama", "dimension": 3,
    }

    def handler(request):
        assert request.url.path == "/v1/embeddings"
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2, 0.3]}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        assert probe_local_embedding(embedding, client) == {"status_code": 200, "dimension": 3}
        embedding["dimension"] = 4
        with pytest.raises(ValueError, match="dimension"):
            probe_local_embedding(embedding, client)
    finally:
        client.close()


def test_s01_event_contract_requires_run_text_search_and_result():
    complete = [
        {"type": "RUN_STARTED"},
        {"type": "TOOL_CALL_START", "toolCallName": "product_search_tool", "toolCallId": "search-1"},
        {"type": "TOOL_CALL_RESULT", "toolCallId": "search-1", "content": "{\"hits\": []}"},
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "找到三个背包"},
        {"type": "RUN_FINISHED"},
    ]
    assert event_contract(complete)["passed"] is True
    for missing in range(1, 4):
        assert event_contract(complete[:missing] + complete[missing + 1:])["passed"] is False


def test_s01_event_contract_rejects_mismatched_or_out_of_order_tool_result():
    prefix = [{"type": "RUN_STARTED"}, {"type": "TEXT_MESSAGE_CONTENT", "delta": "回答"}]
    start = {"type": "TOOL_CALL_START", "toolCallName": "product_search_tool", "toolCallId": "search-A"}
    wrong = {"type": "TOOL_CALL_RESULT", "toolCallId": "other-B", "content": "{}"}
    finish = {"type": "RUN_FINISHED"}
    assert event_contract(prefix + [start, wrong, finish])["passed"] is False
    matched = {"type": "TOOL_CALL_RESULT", "toolCallId": "search-A", "content": "{}"}
    assert event_contract(prefix + [matched, start, finish])["passed"] is False
    whitespace_start = {
        "type": "TOOL_CALL_START", "toolCallName": "product_search_tool", "toolCallId": "   ",
    }
    whitespace_result = {"type": "TOOL_CALL_RESULT", "toolCallId": "   ", "content": "{}"}
    assert event_contract(prefix + [whitespace_start, whitespace_result, finish])["passed"] is False


def test_failure_after_receipt_consumption_still_writes_result_and_manifest(
    tmp_path, monkeypatch, authorization_root,
):
    fake_root = tmp_path / "repo"
    output = fake_root / ".globex-runtime" / "eval" / "failed-after-consume"
    receipt_path = authorization_root / "receipt.json"
    write_receipt(receipt_path, receipt())
    monkeypatch.setattr(driver, "ROOT", fake_root)
    monkeypatch.setattr(driver, "_tracked_clean", lambda: True)
    monkeypatch.setattr(driver, "_source_commit", lambda: "abc123")
    settings = SimpleNamespace(
        llm_api_key="test-only", llm_base_url="https://provider.example/v1",
        embedding_base_url="http://127.0.0.1:11434/v1", embedding_model="bge-m3",
        embedding_version="ollama", embedding_dim=1024,
    )
    monkeypatch.setattr(driver, "load_settings", lambda: settings)
    monkeypatch.setattr(driver, "probe_local_embedding", lambda value: {"status_code": 200, "dimension": 1024})
    original_mkdir = Path.mkdir

    def fail_runtime(path, *args, **kwargs):
        if path.name == "runtime":
            raise OSError("injected runtime mkdir failure")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_runtime)
    result = run_paid(output, receipt_path, "receipt-s01-one")
    assert result["authorization"]["consumed"] is True
    assert result["error_type"] == "OSError"
    assert (output / "result.json").is_file()
    manifest = json.loads((output / "manifest.json").read_text())
    assert any(item["path"] == "result.json" for item in manifest["files"])


def test_local_preflight_is_mock_only_and_rejects_before_reserve(tmp_path):
    output = tmp_path / "fresh-preflight"
    report = local_preflight(output)
    assert report["network"] == "httpx.MockTransport and local loopback only"
    assert report["rejected_status"] == 400
    assert report["reserved_after_rejection"] == 0
    assert report["accepted_status"] == 200
    assert report["upstream_calls"] == 1
    assert report["forwarded_model"] == "deepseek-flash"
    assert report["forwarded_max_tokens"] == 8192
    assert report["forwarded_temperature"] == 0
    assert report["ledger_errors"] == []
    assert report["passed"] is True
    assert (output / "manifest.json").is_file()
