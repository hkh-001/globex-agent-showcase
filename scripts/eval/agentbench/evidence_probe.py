"""Run the runtime evidence probes and write a hash-bound evidence file."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import subprocess
import uuid
from datetime import datetime, timezone

from scripts.eval.agentbench import probes

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=pathlib.Path,
                        default=ROOT / "eval/agent/v1/evidence-probe.json")
    args = parser.parse_args()
    result = asyncio.run(probes.run_all())
    result["run_id"] = uuid.uuid4().hex
    result["generated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        result["source_commit"] = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, capture_output=True, check=True).stdout.strip()
    except Exception:
        result["source_commit"] = "unknown"
    result["scanner_sha256"] = _sha(pathlib.Path(probes.__file__).read_bytes())
    result["records_sha256"] = _sha(json.dumps(result["records"], ensure_ascii=False,
                                               sort_keys=True).encode())
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    passed = [row for row in result["records"] if row["result"] == "pass"]
    print(json.dumps({"run_id": result["run_id"], "records": len(result["records"]),
                      "passed": len(passed), "capabilities": result["capabilities"],
                      "paid_requests": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
