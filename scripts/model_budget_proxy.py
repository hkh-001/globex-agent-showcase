"""Fresh-only local proxy that hard-limits paid chat-completion requests.

The proxy is an acceptance-test boundary, not an application model gateway.  It
stores request metadata only; prompts, tool arguments and authorization headers
are deliberately excluded from the ledger.
"""
from __future__ import annotations

import argparse
import codecs
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx


MAX_BODY_BYTES = 2 * 1024 * 1024
MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}\Z")


class BudgetExhausted(RuntimeError):
    pass


class LedgerUnavailable(RuntimeError):
    pass


class FreshLedger:
    def __init__(self, path: Path, limit: int) -> None:
        if limit < 1:
            raise ValueError("limit must be positive")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.limit = limit
        self._lock = threading.Lock()
        self._count = 0
        self._halted = False
        # A real batch must never append to, overwrite or resume an old ledger.
        with path.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": "opened", "limit": limit}) + "\n")

    def reserve(
        self, *, requested_model: str, requested_output: int | None, applied_output: int,
        temperature: int | float | None = None,
    ) -> tuple[int, str]:
        with self._lock:
            if self._halted:
                raise BudgetExhausted("batch halted after an upstream failure")
            if self._count >= self.limit:
                raise BudgetExhausted(f"physical request limit {self.limit} exhausted")
            self._count += 1
            request_id = uuid.uuid4().hex
            try:
                self._append({
                    "event": "reserved", "sequence": self._count,
                    "request_id": request_id, "requested_model": requested_model,
                    "requested_output": requested_output, "applied_output": applied_output,
                    "temperature": temperature,
                    "at_unix_ns": time.time_ns(),
                })
            except OSError as error:
                self._halted = True
                raise LedgerUnavailable("request ledger write failed; batch halted") from error
            return self._count, request_id

    def finish(
        self, request_id: str, *, status_code: int | None, outcome: str,
        response_metadata: dict | None = None, halt: bool = False,
    ) -> None:
        with self._lock:
            if halt:
                self._halted = True
            try:
                row = {
                    "event": "finished", "request_id": request_id,
                    "status_code": status_code, "outcome": outcome,
                    "batch_halted": halt,
                    "at_unix_ns": time.time_ns(),
                }
                if response_metadata is not None:
                    row["response_metadata"] = response_metadata
                self._append(row)
            except OSError as error:
                self._halted = True
                raise LedgerUnavailable("request ledger write failed; batch halted") from error

    def _append(self, row: dict) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()


def validate_successful_ledger(
    path: Path, *, expected_model: str, expected_output: int,
    expected_temperature: int | float | None = None,
    allowed_served_models: set[str] | None = None,
    minimum_requests: int = 1,
    expected_requests: int | None = None,
) -> list[str]:
    """Return reproducible integrity errors for a completed paid diagnostic ledger."""
    requested_model = validated_model_id(expected_model)
    allowed = {requested_model} if allowed_served_models is None else set(allowed_served_models)
    if not allowed or any(not MODEL_ID_RE.fullmatch(item) for item in allowed):
        raise ValueError("allowed served models must contain valid model identifiers")
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except (OSError, json.JSONDecodeError) as error:
        return [f"ledger_unreadable:{type(error).__name__}"]
    errors: list[str] = []
    opened = [row for row in rows if row.get("event") == "opened"]
    if len(opened) != 1 or not rows or rows[0].get("event") != "opened":
        errors.append("missing_opened_event")
    reserves = [row for row in rows if row.get("event") == "reserved"]
    finishes = [row for row in rows if row.get("event") == "finished"]
    if len(reserves) < minimum_requests:
        errors.append("insufficient_reserved_requests")
    if expected_requests is not None and len(reserves) != expected_requests:
        errors.append("unexpected_reserved_request_count")
    sequences = [row.get("sequence") for row in reserves]
    if sequences != list(range(1, len(reserves) + 1)):
        errors.append("non_contiguous_sequences")
    request_ids = [row.get("request_id") for row in reserves]
    if any(not isinstance(item, str) or not item for item in request_ids) or len(set(request_ids)) != len(request_ids):
        errors.append("invalid_or_duplicate_request_ids")
    finished_by_id: dict[str, list[dict]] = {}
    for row in finishes:
        finished_by_id.setdefault(str(row.get("request_id")), []).append(row)
    reserve_ids = {str(row.get("request_id")) for row in reserves}
    if any(str(row.get("request_id")) not in reserve_ids for row in finishes):
        errors.append("orphan_finished_event")
    for row in reserves:
        sequence = row.get("sequence")
        prefix = f"sequence_{sequence}"
        request_id = str(row.get("request_id"))
        matched = finished_by_id.get(request_id, [])
        if len(matched) != 1:
            errors.append(f"{prefix}:finished_count_{len(matched)}")
            continue
        finished = matched[0]
        if row.get("requested_model") != requested_model:
            errors.append(f"{prefix}:requested_model_mismatch")
        if row.get("applied_output") != expected_output:
            errors.append(f"{prefix}:applied_output_mismatch")
        if row.get("requested_output") != expected_output:
            errors.append(f"{prefix}:requested_output_mismatch")
        if expected_temperature is not None and row.get("temperature") != expected_temperature:
            errors.append(f"{prefix}:temperature_mismatch")
        if finished.get("status_code") != 200 or finished.get("outcome") != "response":
            errors.append(f"{prefix}:unsuccessful_outcome")
        if finished.get("batch_halted") is not False:
            errors.append(f"{prefix}:unexpected_batch_halt")
        metadata = finished.get("response_metadata")
        if not isinstance(metadata, dict) or metadata.get("metadata_complete") is not True:
            errors.append(f"{prefix}:metadata_incomplete")
            continue
        served_model = metadata.get("served_model")
        if not isinstance(served_model, str) or not MODEL_ID_RE.fullmatch(served_model):
            errors.append(f"{prefix}:invalid_served_model")
        elif served_model not in allowed:
            errors.append(f"{prefix}:served_model_mismatch")
        finish_reason = metadata.get("finish_reason")
        if not isinstance(finish_reason, str) or not finish_reason:
            errors.append(f"{prefix}:missing_finish_reason")
        usage = metadata.get("usage")
        if not isinstance(usage, dict) or "total_tokens" not in usage or not (
            "completion_tokens" in usage or "output_tokens" in usage
        ):
            errors.append(f"{prefix}:usage_incomplete")
        elif any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in usage.values()):
            errors.append(f"{prefix}:usage_invalid")
    return errors


def enforce_output_limit(payload: dict, maximum: int) -> tuple[dict, int | None, int]:
    if maximum < 1:
        raise ValueError("maximum output must be positive")
    copied = dict(payload)
    values = []
    for key in ("max_completion_tokens", "max_tokens"):
        value = copied.get(key)
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{key} must be a positive integer")
            values.append(value)
    requested = min(values) if values else None
    applied = min(requested if requested is not None else maximum, maximum)
    copied.pop("max_completion_tokens", None)
    copied["max_tokens"] = applied
    return copied, requested, applied


def validated_model_id(value: object) -> str:
    if not isinstance(value, str) or not MODEL_ID_RE.fullmatch(value):
        raise ValueError("model must be a non-empty model identifier of at most 200 characters")
    return value


class ResponseMetadataObserver:
    """Extract non-content response facts while the proxy forwards bytes unchanged."""

    _USAGE_FIELDS = {
        "prompt_tokens", "completion_tokens", "total_tokens",
        "input_tokens", "output_tokens",
        "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
    }

    def __init__(self, content_type: str) -> None:
        self.content_type = content_type.lower()
        self._json = bytearray()
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._text = ""
        self.models: set[str] = set()
        self.finish_reasons: set[str] = set()
        self.usage: dict[str, int] = {}
        self.parse_error = False
        self.events_seen = 0
        self._observed_bytes = 0

    def feed(self, chunk: bytes) -> None:
        self._observed_bytes += len(chunk)
        if self._observed_bytes > MAX_BODY_BYTES:
            self.parse_error = True
            self._json.clear()
            self._text = ""
            return
        if self.parse_error:
            return
        if "application/json" in self.content_type:
            self._json.extend(chunk)
            return
        try:
            self._text += self._decoder.decode(chunk)
            while "\n" in self._text:
                line, self._text = self._text.split("\n", 1)
                self._sse_line(line.rstrip("\r"))
        except UnicodeDecodeError:
            self.parse_error = True

    def finish(self) -> dict:
        if "application/json" in self.content_type:
            if self._json and not self.parse_error:
                try:
                    payload = json.loads(self._json)
                    if isinstance(payload, dict):
                        self._observe(payload)
                    else:
                        self.parse_error = True
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self.parse_error = True
        else:
            try:
                self._text += self._decoder.decode(b"", final=True)
                if self._text:
                    self._sse_line(self._text.rstrip("\r\n"))
            except UnicodeDecodeError:
                self.parse_error = True
        usage_complete = (
            "total_tokens" in self.usage
            and ("completion_tokens" in self.usage or "output_tokens" in self.usage)
        )
        metadata_complete = (
            not self.parse_error
            and len(self.models) == 1
            and len(self.finish_reasons) == 1
            and usage_complete
        )
        return {
            "content_type": self.content_type.split(";", 1)[0].strip(),
            "events_seen": self.events_seen,
            "served_model": next(iter(self.models)) if len(self.models) == 1 else None,
            "finish_reason": next(iter(self.finish_reasons)) if len(self.finish_reasons) == 1 else None,
            "usage": dict(sorted(self.usage.items())),
            "metadata_complete": metadata_complete,
        }

    def _sse_line(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if not data or data == "[DONE]":
            return
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            self.parse_error = True
            return
        if isinstance(payload, dict):
            self._observe(payload)

    def _observe(self, payload: dict) -> None:
        self.events_seen += 1
        model = payload.get("model")
        if isinstance(model, str) and MODEL_ID_RE.fullmatch(model):
            self.models.add(model)
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                reason = choice.get("finish_reason") if isinstance(choice, dict) else None
                if isinstance(reason, str) and 0 < len(reason) <= 80:
                    self.finish_reasons.add(reason)
        usage = payload.get("usage")
        if isinstance(usage, dict):
            for key in self._USAGE_FIELDS:
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    self.usage[key] = value


class BudgetProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GlobexBudgetProxy/1"

    def log_message(self, _format: str, *_args) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        server: BudgetProxyServer = self.server  # type: ignore[assignment]
        if self.path.rstrip("/") != "/chat/completions":
            self._json_error(404, "unsupported_path")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._json_error(400, "invalid_content_length")
            return
        if length < 1 or length > MAX_BODY_BYTES:
            self._json_error(413, "invalid_body_size")
            return
        try:
            payload = json.loads(self.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            requested_model = validated_model_id(payload.get("model"))
            payload, requested, applied = enforce_output_limit(payload, server.maximum_output)
            raw_temperature = payload.get("temperature")
            if raw_temperature is not None and (
                isinstance(raw_temperature, bool) or not isinstance(raw_temperature, (int, float))
            ):
                raise ValueError("temperature must be numeric when provided")
            temperature = raw_temperature
            if temperature is None and server.expected_temperature is not None:
                temperature = server.expected_temperature
                payload["temperature"] = temperature
            if server.expected_model is not None and requested_model != server.expected_model:
                raise ValueError("requested model does not match the frozen batch model")
            if server.expected_temperature is not None and temperature != server.expected_temperature:
                raise ValueError("temperature does not match the frozen batch temperature")
            if server.expected_output is not None and (
                requested != server.expected_output or applied != server.expected_output
            ):
                raise ValueError("requested output does not match the frozen batch output")
        except (json.JSONDecodeError, ValueError) as error:
            self._json_error(400, str(error))
            return

        headers = {"content-type": "application/json"}
        authorization = self.headers.get("Authorization")
        if authorization:
            headers["authorization"] = authorization
        # Paid diagnostic batches are deliberately serialized.  The lock spans
        # reserve -> upstream -> durable finish so a failure cannot race a later call.
        with server.request_lock:
            try:
                _, request_id = server.ledger.reserve(
                    requested_model=requested_model, requested_output=requested,
                    applied_output=applied, temperature=temperature,
                )
            except BudgetExhausted:
                self._json_error(429, "physical_request_budget_exhausted")
                return
            except LedgerUnavailable:
                self._json_error(503, "request_ledger_unavailable_batch_halted")
                return

            status = None
            try:
                with server.client.stream("POST", server.upstream, headers=headers, json=payload) as response:
                    status = response.status_code
                    observer = ResponseMetadataObserver(response.headers.get("content-type", ""))
                    self.close_connection = True
                    self.send_response(status)
                    self.send_header("Content-Type", response.headers.get("content-type", "application/json"))
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    # httpx decodes gzip/br here.  Forwarding raw compressed bytes without
                    # Content-Encoding would corrupt the downstream JSON/SSE stream.
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        observer.feed(chunk)
                        self.wfile.write(f"{len(chunk):X}\r\n".encode("ascii"))
                        self.wfile.write(chunk)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    response_metadata = observer.finish()
                metadata_incomplete = status < 400 and not response_metadata["metadata_complete"]
                model_mismatch = (
                    status < 400 and response_metadata["metadata_complete"]
                    and response_metadata["served_model"] != requested_model
                )
                server.ledger.finish(
                    request_id, status_code=status,
                    outcome=(
                        "response_metadata_incomplete" if metadata_incomplete
                        else "served_model_mismatch" if model_mismatch
                        else "response"
                    ),
                    response_metadata=response_metadata,
                    halt=status >= 400 or metadata_incomplete or model_mismatch,
                )
            except LedgerUnavailable:
                # The response may already be complete, but no later request may start.
                self.close_connection = True
            except Exception:  # the ledger must retain an unknown paid outcome
                try:
                    server.ledger.finish(
                        request_id, status_code=status, outcome="transport_unknown", halt=True,
                    )
                except LedgerUnavailable:
                    pass
                self.close_connection = True
                if status is None and not self.wfile.closed:
                    self._json_error(502, "upstream_transport_unknown_batch_halted")

    def _json_error(self, status: int, code: str) -> None:
        body = json.dumps({"error": {"code": code}}, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class BudgetProxyServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, address, *, upstream: str, ledger: FreshLedger, maximum_output: int,
        expected_model: str,
        expected_temperature: int | float | None = None,
        expected_output: int | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        parsed = urlsplit(upstream)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("upstream must be an HTTPS origin without credentials")
        self.upstream = upstream.rstrip("/") + "/chat/completions"
        self.ledger = ledger
        self.maximum_output = maximum_output
        self.expected_model = validated_model_id(expected_model)
        if isinstance(expected_temperature, bool) or (
            expected_temperature is not None and not isinstance(expected_temperature, (int, float))
        ):
            raise ValueError("expected temperature must be numeric")
        self.expected_temperature = expected_temperature
        if expected_output is not None and (
            isinstance(expected_output, bool) or not isinstance(expected_output, int) or expected_output < 1
        ):
            raise ValueError("expected output must be a positive integer")
        if expected_output is not None and expected_output > maximum_output:
            raise ValueError("expected output cannot exceed maximum output")
        self.expected_output = expected_output
        self.request_lock = threading.Lock()
        self.client = client or httpx.Client(timeout=120.0, follow_redirects=False, trust_env=False)
        self._owns_client = client is None
        super().__init__(address, BudgetProxyHandler)

    def server_close(self) -> None:
        if self._owns_client:
            self.client.close()
        super().server_close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="127.0.0.1")
    parser.add_argument("--listen-port", type=int, required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--maximum-output", type=int, default=1024)
    parser.add_argument("--expected-model", required=True)
    parser.add_argument("--expected-temperature", type=float, default=0)
    parser.add_argument("--expected-output", type=int, required=True)
    args = parser.parse_args()
    ledger = FreshLedger(args.ledger, args.limit)
    server = BudgetProxyServer(
        (args.listen_host, args.listen_port), upstream=args.upstream,
        ledger=ledger, maximum_output=args.maximum_output,
        expected_model=args.expected_model, expected_temperature=args.expected_temperature,
        expected_output=args.expected_output,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
