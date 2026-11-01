"""Reproducible S01 paid canary driver; defaults to a zero-network preflight.

The ``run`` mode is intentionally difficult to invoke accidentally: it requires
a fresh human-authorization receipt plus an exact acknowledgement.  It starts a
fresh budget proxy and an isolated backend, performs exactly one user turn, then
drains both processes and validates the physical-request ledger.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.infrastructure.settings import load_settings
from scripts.eval.harness.contracts import ROOT, assert_output_path, write_json
from scripts.model_budget_proxy import (
    BudgetProxyServer,
    FreshLedger,
    validate_successful_ledger,
)


CASE_ID = "S01"
QUERY = "帮我挑一个适合旅行的背包。"
MODEL = "deepseek-flash"
OUTPUT_TOKENS = 8192
TEMPERATURE = 0
RECEIPT_SCHEMA = "globex-paid-eval-authorization-v1"
RESULT_SCHEMA = "globex-s01-canary-run-v3"
AUTHORIZATION_ROOT = ROOT / ".globex-runtime" / "authorizations"
CONSUMED_ROOT = AUTHORIZATION_ROOT / "consumed"
RECEIPT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{7,127}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_commit() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
        capture_output=True, text=True,
    ).stdout.strip()


def _tracked_clean() -> bool:
    unstaged = subprocess.run(["git", "diff", "--quiet", "HEAD"], cwd=ROOT).returncode == 0
    staged = subprocess.run(["git", "diff", "--cached", "--quiet", "HEAD"], cwd=ROOT).returncode == 0
    return unstaged and staged


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _reserved_count(path: Path) -> int:
    return sum(row.get("event") == "reserved" for row in _rows(path))


def validate_receipt(path: Path, acknowledgement: str, source_commit: str,
                     case_id: str = CASE_ID, max_physical_requests: int = 4) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError("authorization receipt must not be a symlink")
    if not path.is_file():
        raise ValueError("authorization receipt does not exist")
    resolved = path.resolve()
    if resolved.parent != AUTHORIZATION_ROOT.resolve():
        raise ValueError("authorization receipt must be a direct file in .globex-runtime/authorizations")
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", str(resolved)], cwd=ROOT,
    ).returncode == 0
    if not ignored:
        raise ValueError("authorization receipt must be gitignored")
    if path.stat().st_mode & 0o077:
        raise ValueError("authorization receipt permissions must not allow group/other access")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    exact = {
        "schema_version": RECEIPT_SCHEMA,
        "authorized": True,
        "case_id": case_id,
        "source_commit": source_commit,
        "model": MODEL,
        "temperature": TEMPERATURE,
        "max_output_tokens_per_request": OUTPUT_TOKENS,
        "max_physical_requests": max_physical_requests,
        "automatic_retries": 0,
        "fallback_model": "",
    }
    for key, value in exact.items():
        if receipt.get(key) != value:
            raise ValueError(f"authorization receipt field mismatch: {key}")
    receipt_id = receipt.get("receipt_id")
    if not isinstance(receipt_id, str) or not RECEIPT_ID_RE.fullmatch(receipt_id) or acknowledgement != receipt_id:
        raise ValueError("paid-run acknowledgement must exactly match receipt_id")
    expires_at = receipt.get("expires_at")
    if not isinstance(expires_at, str):
        raise ValueError("authorization receipt requires expires_at")
    expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise ValueError("authorization receipt is expired or timezone-naive")
    return receipt


def consume_receipt(path: Path, receipt: dict[str, Any], output: Path) -> Path:
    if AUTHORIZATION_ROOT.is_symlink() or CONSUMED_ROOT.is_symlink():
        raise ValueError("authorization directories must not be symlinks")
    CONSUMED_ROOT.mkdir(parents=True, exist_ok=True)
    CONSUMED_ROOT.chmod(0o700)
    marker_name = hashlib.sha256(receipt["receipt_id"].encode("utf-8")).hexdigest() + ".json"
    marker = CONSUMED_ROOT / marker_name
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({
            "receipt_id": receipt["receipt_id"],
            "receipt_sha256": _sha256(path),
            "source_commit": receipt["source_commit"],
            "output": str(output),
            "consumed_at": datetime.now(timezone.utc).isoformat(),
        }, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    return marker


def validate_local_embedding(settings: Any) -> dict[str, Any]:
    parsed = urlsplit(settings.embedding_base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("S01 requires a loopback-only HTTP embedding service")
    if parsed.username or parsed.password:
        raise ValueError("local embedding URL must not contain credentials")
    if settings.embedding_model != "bge-m3" or settings.embedding_dim != 1024:
        raise ValueError("S01 requires local bge-m3 with 1024 dimensions")
    return {
        "base_url": settings.embedding_base_url.rstrip("/"),
        "model": settings.embedding_model,
        "version": settings.embedding_version,
        "dimension": settings.embedding_dim,
    }


def probe_local_embedding(embedding: dict[str, Any], client: httpx.Client | None = None) -> dict[str, Any]:
    owned = client is None
    http = client or httpx.Client(trust_env=False, timeout=60)
    try:
        response = http.post(
            embedding["base_url"] + "/embeddings",
            json={"model": embedding["model"], "input": ["Globex S01 local preflight"]},
            headers={"Authorization": "Bearer local-only"},
        )
        response.raise_for_status()
        body = response.json()
        vector = body.get("data", [{}])[0].get("embedding")
        if not isinstance(vector, list) or len(vector) != embedding["dimension"]:
            raise ValueError("local embedding probe returned an unexpected dimension")
        return {"status_code": response.status_code, "dimension": len(vector)}
    finally:
        if owned:
            http.close()


def proxy_command(*, port: int, upstream: str, ledger: Path, limit: int) -> list[str]:
    return [
        sys.executable, str(ROOT / "scripts/model_budget_proxy.py"),
        "--listen-port", str(port), "--upstream", upstream,
        "--ledger", str(ledger), "--limit", str(limit),
        "--maximum-output", str(OUTPUT_TOKENS),
        "--expected-output", str(OUTPUT_TOKENS),
        "--expected-model", MODEL,
        "--expected-temperature", str(TEMPERATURE),
    ]


def backend_environment(*, proxy_port: int, backend_port: int, data_dir: Path, api_key: str,
                        embedding: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "PORT": str(backend_port),
        "DATA_DIR": str(data_dir),
        "LLM_BASE_URL": f"http://127.0.0.1:{proxy_port}/v1",
        "LLM_API_KEY": api_key,
        "LLM_MODEL": MODEL,
        "LLM_FALLBACK_MODEL": "",
        "LLM_MAX_RETRIES": "0",
        "LLM_MAX_CONCURRENCY": "1",
        "LLM_MIN_INTERVAL_SECONDS": "0",
        "TOKEN_BUDGET_TOTAL": "0",
        "REPLY_TOKEN_BUDGET": "0",
        "QUEUE_ENABLED": "0",
        "REDIS_URL": "",
        "SEMANTIC_CACHE_ENABLED": "0",
        "PROMPT_CACHE_MODE": "passthrough",
        "MODEL_RECOVERY_MODE": "off",
        "LLM_ENABLE_THINKING": "0",
        "RERANKER_MODE": "disabled",
        "RERANKER_API_KEY": "",
        "RERANKER_BASE_URL": "",
        "RERANKER_MODEL": "",
        "TAVILY_API_KEY": "",
        "PREFERENCE_RELEVANCE_ENABLED": "0",
        "DRIFT_DETECT_ENABLED": "0",
        "OTEL_SDK_DISABLED": "true",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "",
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "",
        "OTEL_EXPORTER_OTLP_HEADERS": "",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "",
        "LANGFUSE_BASE_URL": "",
        "LANGFUSE_PUBLIC_KEY": "",
        "LANGFUSE_SECRET_KEY": "",
        "EMBEDDING_BASE_URL": embedding["base_url"],
        "EMBEDDING_API_KEY": "local-only",
        "EMBEDDING_MODEL": embedding["model"],
        "EMBEDDING_VERSION": embedding["version"],
        "EMBEDDING_DIM": str(embedding["dimension"]),
        "DATABASE_URL": "",
        "MYSQL_URL": "",
        "IDENTITY_MODE": "demo",
        "MODEL_ALLOWED_LOCAL_HOSTS": "127.0.0.1,localhost",
    })
    for name in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        env.pop(name, None)
    return env


def _complete_mock_response() -> dict[str, Any]:
    return {
        "id": "preflight", "model": MODEL,
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
    }


def local_preflight(output: Path) -> dict[str, Any]:
    """Exercise the exact proxy contract through MockTransport; never touches a provider."""
    output = assert_output_path(output)
    output.mkdir(parents=True)
    upstream: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        upstream.append(json.loads(request.content))
        return httpx.Response(200, json=_complete_mock_response())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    ledger_path = output / "mock-ledger.jsonl"
    ledger = FreshLedger(ledger_path, 1)
    server = BudgetProxyServer(
        ("127.0.0.1", 0), upstream="https://provider.invalid/v1",
        ledger=ledger, maximum_output=OUTPUT_TOKENS,
        expected_output=OUTPUT_TOKENS, expected_model=MODEL,
        expected_temperature=TEMPERATURE, client=client,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/chat/completions"
    try:
        rejected = httpx.post(url, json={
            "model": MODEL + "-x", "max_tokens": OUTPUT_TOKENS,
        }, timeout=5)
        rows_after_rejection = _rows(ledger_path)
        accepted = httpx.post(url, json={
            "model": MODEL, "max_tokens": OUTPUT_TOKENS,
            "messages": [{"role": "user", "content": "local mock only"}],
        }, timeout=5)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
    errors = validate_successful_ledger(
        ledger_path, expected_model=MODEL, expected_output=OUTPUT_TOKENS,
        expected_temperature=TEMPERATURE, expected_requests=1,
    )
    report = {
        "schema_version": "globex-s01-canary-preflight-v2",
        "source_commit": _source_commit(),
        "network": "httpx.MockTransport and local loopback only",
        "rejected_status": rejected.status_code,
        "rows_after_rejection": len(rows_after_rejection),
        "reserved_after_rejection": sum(row.get("event") == "reserved" for row in rows_after_rejection),
        "accepted_status": accepted.status_code,
        "upstream_calls": len(upstream),
        "forwarded_model": upstream[0].get("model") if upstream else None,
        "forwarded_max_tokens": upstream[0].get("max_tokens") if upstream else None,
        "forwarded_temperature": upstream[0].get("temperature") if upstream else None,
        "ledger_errors": errors,
        "passed": (
            rejected.status_code == 400 and len(rows_after_rejection) == 1
            and accepted.status_code == 200 and len(upstream) == 1 and not errors
        ),
    }
    write_json(output / "report.json", report)
    _write_manifest(output, [output / "report.json", ledger_path], report["source_commit"])
    return report


def _wait_http(url: str, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = "not_started"
    with httpx.Client(trust_env=False, timeout=5) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get(url)
                last = f"http_{response.status_code}"
                if response.status_code == 200:
                    return response.json()
            except (httpx.HTTPError, ValueError) as error:
                last = type(error).__name__
            time.sleep(0.5)
    raise RuntimeError(f"service readiness timeout: {last}")


def _wait_proxy_rejection(url: str, ledger: Path, *, timeout: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    with httpx.Client(trust_env=False, timeout=5) as client:
        while time.monotonic() < deadline:
            try:
                response = client.post(url, json={
                    "model": MODEL + "-x", "max_tokens": OUTPUT_TOKENS,
                })
                if response.status_code == 400:
                    rows = _rows(ledger)
                    if len(rows) != 1 or rows[0].get("event") != "opened":
                        raise RuntimeError("rejection self-test touched the paid ledger")
                    return {"status_code": 400, "ledger_rows": 1, "reserved": 0}
                raise RuntimeError(f"unexpected proxy self-test status: {response.status_code}")
            except httpx.ConnectError:
                time.sleep(0.2)
    raise RuntimeError("budget proxy readiness timeout")


def _terminate(process: subprocess.Popen | None, *, timeout: float = 20) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _parse_sse(response: httpx.Response) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in response.iter_lines():
        if line.startswith("data: "):
            payload = json.loads(line[6:])
            if isinstance(payload, dict):
                events.append(payload)
    return events


def event_contract(events: list[dict[str, Any]]) -> dict[str, Any]:
    types = [event.get("type") for event in events]
    text = "".join(
        str(event.get("delta", "")) for event in events
        if event.get("type") == "TEXT_MESSAGE_CONTENT"
    ).strip()
    search_starts = {
        event.get("toolCallId"): index for index, event in enumerate(events)
        if event.get("type") == "TOOL_CALL_START"
        and event.get("toolCallName") == "product_search_tool"
        and isinstance(event.get("toolCallId"), str) and event.get("toolCallId").strip()
    }
    paired_search_results = [
        event for index, event in enumerate(events)
        if event.get("type") == "TOOL_CALL_RESULT"
        and event.get("toolCallId") in search_starts
        and index > search_starts[event.get("toolCallId")]
        and bool(str(event.get("content", "")).strip())
    ]
    checks = {
        "run_started": bool(types) and types[0] == "RUN_STARTED",
        "run_finished": bool(types) and types[-1] == "RUN_FINISHED",
        "assistant_text_present": bool(text),
        "product_search_started": bool(search_starts),
        "product_search_result_paired": bool(paired_search_results),
    }
    return {"checks": checks, "passed": all(checks.values()), "assistant_text_chars": len(text)}


def _write_manifest(output: Path, files: list[Path], source_commit: str) -> None:
    manifest = {
        "schema_version": "globex-s01-canary-evidence-manifest-v2",
        "source_commit": source_commit,
        "files": [
            {"path": str(path.relative_to(output)), "bytes": path.stat().st_size, "sha256": _sha256(path)}
            for path in files
        ],
        "excluded": [
            {"path": "runtime", "reason": "isolated SQLite/Qdrant runtime; authoritative counts are copied into result.json"},
        ],
    }
    write_json(output / "manifest.json", manifest)


def _safe_upstream(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("paid upstream must be an HTTPS URL without embedded credentials")
    if parsed.hostname in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("paid upstream cannot be the local evaluation proxy")
    return base_url.rstrip("/")


def _ag_ui_request(case_id: str = CASE_ID, query: str = QUERY) -> dict[str, Any]:
    slug = re.sub(r"[^a-z0-9-]+", "-", case_id.lower()).strip("-") or "case"
    return {
        "threadId": f"eval-{slug}-deepseek-flash-8192",
        "runId": f"eval-{slug}-deepseek-flash-8192-run-1",
        "state": {},
        "messages": [{"id": f"{slug}-user-1", "role": "user", "content": query}],
        "tools": [], "context": [],
        "forwardedProps": {"buyerId": f"eval-buyer-{slug}", "locale": "zh-CN", "currency": "CNY"},
    }


def run_paid(output: Path, receipt_path: Path, acknowledgement: str, *,
             case_id: str = CASE_ID, query: str = QUERY,
             max_physical_requests: int = 4, runtime_dir: Path | None = None) -> dict[str, Any]:
    output = assert_output_path(output)
    if ROOT / ".globex-runtime" / "eval" not in output.parents:
        raise ValueError("paid evidence output must be under .globex-runtime/eval")
    if not _tracked_clean():
        raise ValueError("tracked worktree must be clean before a paid run")
    source_commit = _source_commit()
    receipt = validate_receipt(
        receipt_path, acknowledgement, source_commit,
        case_id=case_id, max_physical_requests=max_physical_requests,
    )
    settings = load_settings()
    if not settings.llm_api_key:
        raise ValueError("LLM_API_KEY is missing")
    upstream = _safe_upstream(settings.llm_base_url)
    embedding = validate_local_embedding(settings)
    embedding_probe = probe_local_embedding(embedding)
    receipt_sha256 = _sha256(receipt_path)
    output.mkdir(parents=True)
    runtime = Path(runtime_dir) if runtime_dir is not None else output / "runtime"
    if runtime_dir is not None and (not runtime.is_dir() or not any(runtime.iterdir())):
        raise ValueError("reused runtime directory must exist and be non-empty")
    ledger = output / "http-ledger.jsonl"
    proxy_port, backend_port = _port(), _port()
    while backend_port == proxy_port:
        backend_port = _port()
    proxy_url = f"http://127.0.0.1:{proxy_port}/chat/completions"
    backend_url = f"http://127.0.0.1:{backend_port}"
    proxy_process: subprocess.Popen | None = None
    backend_process: subprocess.Popen | None = None
    marker: Path | None = None
    files: list[Path] = []
    result: dict[str, Any] = {
        "schema_version": RESULT_SCHEMA,
        "source_commit": source_commit,
        "case_id": case_id,
        "status": "infrastructure_failure",
        "paid_request_attempted": False,
        "authorization": {
            "receipt_id": receipt["receipt_id"],
            "receipt_sha256": receipt_sha256,
            "consumed": False,
            "consumed_marker_sha256": None,
            "contract": {key: receipt[key] for key in (
                "case_id", "source_commit", "model", "temperature",
                "max_output_tokens_per_request", "max_physical_requests",
                "automatic_retries", "fallback_model", "expires_at",
            )},
        },
        "local_embedding": embedding,
        "local_embedding_probe": embedding_probe,
    }
    try:
        marker = consume_receipt(receipt_path, receipt, output)
        result["authorization"].update({
            "consumed": True,
            "consumed_marker_sha256": _sha256(marker),
        })
        if runtime_dir is None:
            runtime.mkdir()
        with (output / "proxy.log").open("wb") as proxy_log:
            proxy_process = subprocess.Popen(
                proxy_command(port=proxy_port, upstream=upstream, ledger=ledger,
                              limit=receipt["max_physical_requests"]),
                cwd=ROOT, stdout=proxy_log, stderr=subprocess.STDOUT,
                env={k: v for k, v in os.environ.items() if not k.lower().endswith("proxy")},
            )
            result["proxy_negative_self_test"] = _wait_proxy_rejection(proxy_url, ledger, timeout=15)
            with (output / "backend.log").open("wb") as backend_log:
                backend_process = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", "app.presentation.server:app",
                     "--host", "127.0.0.1", "--port", str(backend_port), "--workers", "1"],
                    cwd=ROOT, stdout=backend_log, stderr=subprocess.STDOUT,
                    env=backend_environment(proxy_port=proxy_port, backend_port=backend_port,
                                            data_dir=runtime, api_key=settings.llm_api_key,
                                            embedding=embedding),
                )
                result["health"] = _wait_http(backend_url + "/health", timeout=300)
                if _reserved_count(ledger) != 0:
                    raise RuntimeError("backend startup unexpectedly used the chat model")
                request_body = _ag_ui_request(case_id, query)
                write_json(output / "request.json", request_body)
                files.append(output / "request.json")
                result["paid_request_attempted"] = True
                with httpx.Client(trust_env=False, timeout=600) as http:
                    with http.stream("POST", backend_url + "/commerce/ag-ui/run", json=request_body) as response:
                        result["http_status"] = response.status_code
                        events = _parse_sse(response)
                    response.raise_for_status()
                    confirmations_response = http.get(
                        backend_url + "/commerce/confirmations",
                        params={
                            "buyer_id": request_body["forwardedProps"]["buyerId"],
                            "session_id": request_body["threadId"],
                        },
                    )
                    confirmations_response.raise_for_status()
                    orders_response = http.get(
                        backend_url + "/commerce/orders",
                        params={"buyer_id": request_body["forwardedProps"]["buyerId"]},
                    )
                    orders_response.raise_for_status()
                    side_effects = {
                        "confirmations": confirmations_response.json(),
                        "orders": orders_response.json(),
                    }
                write_json(output / "events.json", events)
                write_json(output / "side-effects.json", side_effects)
                files.extend([output / "events.json", output / "side-effects.json"])
        _terminate(backend_process)
        _terminate(proxy_process)
        rows = _rows(ledger)
        request_count = _reserved_count(ledger)
        ledger_errors = validate_successful_ledger(
            ledger, expected_model=MODEL, expected_output=OUTPUT_TOKENS,
            expected_temperature=TEMPERATURE, expected_requests=request_count,
        )
        if not 1 <= request_count <= receipt["max_physical_requests"]:
            ledger_errors.append("physical_request_count_out_of_authorized_range")
        confirmations = side_effects.get("confirmations", {}).get("confirmations", [])
        orders = side_effects.get("orders", {}).get("orders", side_effects.get("orders", []))
        result.update({
            "physical_requests": request_count,
            "ledger_errors": ledger_errors,
            "event_count": len(events),
            "event_contract": event_contract(events),
            "no_trade_side_effects": not confirmations and not orders,
            "status": (
                "captured" if not ledger_errors and not confirmations and not orders
                and event_contract(events)["passed"] else "failed_contract"
            ),
        })
        files.append(ledger)
    except Exception as error:  # preserve first failure; never retry the user turn
        result["error_type"] = type(error).__name__
        result["error"] = str(error)[:500]
    finally:
        _terminate(backend_process)
        _terminate(proxy_process)
        result["backend_exit_code"] = backend_process.poll() if backend_process is not None else None
        result["proxy_exit_code"] = proxy_process.poll() if proxy_process is not None else None
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
        write_json(output / "result.json", result)
        files.append(output / "result.json")
        for evidence in (ledger, output / "backend.log", output / "proxy.log"):
            if evidence.is_file():
                files.append(evidence)
        existing = list(dict.fromkeys(path for path in files if path.is_file()))
        _write_manifest(output, existing, source_commit)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="mode", required=True)
    preflight = sub.add_parser("preflight", help="local MockTransport only; zero provider calls")
    preflight.add_argument("--output", type=Path, required=True)
    run = sub.add_parser("run", help="one authorized paid S01 turn")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--authorization-receipt", type=Path, required=True)
    run.add_argument("--acknowledge-paid-run", required=True)
    run.add_argument("--case-id", default=CASE_ID)
    run.add_argument("--query", default=QUERY)
    run.add_argument("--max-physical-requests", type=int, default=4)
    run.add_argument("--runtime-dir", type=Path)
    args = parser.parse_args()
    if args.mode == "preflight":
        report = local_preflight(args.output)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 2
    result = run_paid(
        args.output, args.authorization_receipt, args.acknowledge_paid_run,
        case_id=args.case_id, query=args.query,
        max_physical_requests=args.max_physical_requests, runtime_dir=args.runtime_dir,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "captured" else 2


if __name__ == "__main__":
    raise SystemExit(main())
