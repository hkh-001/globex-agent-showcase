"""Private audit trail for answers replaced by the grounding gate.

When the product answer is rewritten from verified tool facts, the model text is
discarded before it reaches the client. Without a local record there is no way
to tell a correct block from a false positive, so keep the discarded text in a
local-only JSONL file under the data directory (never sent to the client, never
committed).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


def record(
    path: Path,
    *,
    session_id: str,
    violations: list[str],
    original: str,
    replacement: str,
    kind: str = "grounded_product_fallback",
) -> None:
    """Append one replacement record. Raises OSError when the file is unwritable."""
    entry = {
        "at": time.time(),
        "session_id": session_id,
        "violations": list(violations),
        "original_chars": len(original),
        "original_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
        "original_text": original,
        "replacement_sha256": hashlib.sha256(replacement.encode("utf-8")).hexdigest(),
        "replacement_kind": kind,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
