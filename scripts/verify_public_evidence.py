from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "eval" / "verification" / "public"
MANIFEST = EVIDENCE / "manifest.json"


def main() -> int:
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected = {entry["path"]: entry for entry in payload["files"]}
    actual = {
        path.relative_to(EVIDENCE).as_posix(): path
        for path in EVIDENCE.rglob("*")
        if path.is_file() and path != MANIFEST
    }
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatches: list[str] = []
    for relative, entry in expected.items():
        path = actual.get(relative)
        if path is None:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != entry["sha256"] or path.stat().st_size != entry["bytes"]:
            mismatches.append(relative)
    if missing or extra or mismatches:
        print(json.dumps({"missing": missing, "extra": extra, "mismatches": mismatches}, ensure_ascii=False))
        return 1
    print(f"verified {len(actual)} public evidence files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
