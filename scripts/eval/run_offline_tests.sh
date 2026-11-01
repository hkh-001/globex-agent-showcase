#!/usr/bin/env bash
# Reproducible offline test entry: no proxies, dummy key, no external paid/model calls.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
unset ALL_PROXY all_proxy HTTP_PROXY http_proxy HTTPS_PROXY https_proxy NO_PROXY no_proxy
export NO_PROXY=127.0.0.1,localhost
export LLM_API_KEY="${LLM_API_KEY:-sk-offline-dummy}"
export TAVILY_API_KEY=""
export PYTHONDONTWRITEBYTECODE=1
PY="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi
exec "$PY" -m pytest tests/ -q -p no:cacheprovider "$@"
