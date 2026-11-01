"""Pytest plugin that blocks and records external INET connections."""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import socket
from threading import Lock


_original_connect = None
_original_connect_ex = None
_lock = Lock()


def _classify_address(address) -> str:
    if not isinstance(address, tuple) or not address:
        return "local_socket"
    host = address[0]
    try:
        parsed = ipaddress.ip_address(str(host).split("%", 1)[0])
    except ValueError:
        return "external"
    return "loopback" if parsed.is_loopback else "external"


def _record(kind: str, operation: str) -> None:
    path = Path(os.environ["AGENTBENCH_NETWORK_GUARD_LOG"])
    row = {"kind": kind, "operation": operation}
    with _lock, path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")


def pytest_configure(config):
    global _original_connect, _original_connect_ex
    log = os.environ.get("AGENTBENCH_NETWORK_GUARD_LOG")
    if not log:
        raise RuntimeError("AGENTBENCH_NETWORK_GUARD_LOG is required")
    path = Path(log)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    path.chmod(0o600)
    _original_connect = socket.socket.connect
    _original_connect_ex = socket.socket.connect_ex

    def guarded_connect(instance, address):
        kind = _classify_address(address)
        _record(kind, "connect")
        if kind == "external":
            raise OSError("AgentBench Stage 0 blocks external network connections")
        return _original_connect(instance, address)

    def guarded_connect_ex(instance, address):
        kind = _classify_address(address)
        _record(kind, "connect_ex")
        if kind == "external":
            return 1
        return _original_connect_ex(instance, address)

    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex


def pytest_unconfigure(config):
    if _original_connect is not None:
        socket.socket.connect = _original_connect
    if _original_connect_ex is not None:
        socket.socket.connect_ex = _original_connect_ex
