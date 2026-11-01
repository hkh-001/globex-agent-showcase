"""Make the test suite independent of shell proxy/model environment."""
from __future__ import annotations

import os

for name in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
             "HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"):
    os.environ.pop(name, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ.setdefault("LLM_API_KEY", "sk-offline-dummy")
os.environ.setdefault("TAVILY_API_KEY", "")
