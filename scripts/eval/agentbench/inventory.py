"""Schema-aware AgentBench Stage 1 asset inventory.

Every asset declares its own id field, normalization and version family.  The
scanner reports raw record counts and effective case counts (versions
collapsed), plus hashes, so the numbers are reproducible instead of hand-copied.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
from datetime import datetime, timezone

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCAN_VERSION = "agentbench-inventory-v1"

# path, kind, id_field, id_normalization, family, version, deterministic, prompt_dependent
ASSETS = [
    ("eval/product_recall.jsonl", "retrieval_legacy", None, None, "retrieval-legacy", "v0", True, False),
    ("eval/v1/product_retrieval.jsonl", "retrieval", "id", None, "retrieval-v1", "v1", True, False),
    ("eval/v2/product_retrieval.jsonl", "retrieval", "id", None, "retrieval-v2", "v1", True, False),
    ("eval/v3/multilingual_retrieval.jsonl", "retrieval_multilingual", "id", None, "retrieval-v3", "v1", True, False),
    ("eval/v1/knowledge_retrieval.jsonl", "knowledge", "id", None, "knowledge-v1", "v1", True, False),
    ("eval/business/public-cases-v1.json", "business_contract", "id", None, "business-public", "v1", True, False),
    ("eval/cases.yaml", "model_eval", "id", None, "model-eval", "v1", False, True),
    ("eval/v1/agent_cases.yaml", "agent_case", "id", None, "agent-cases-v1", "v1", False, True),
    ("eval/harness/v1/cases.json", "harness_case", "id", None, "harness-cases", "v1-base", False, True),
    ("eval/harness/v1/skill-catalog-cases.json", "harness_case", "id", None, "harness-cases", "v1-skill", False, True),
    ("eval/harness/v1/cache-layout-cases.json", "harness_case", "id", None, "harness-cases", "v1-cache", False, True),
    ("eval/context/cases.json", "context_case", "id", r"^v\d+-", "context-cases", "v1", False, True),
    ("eval/context/cases-v2.json", "context_case", "id", r"^v\d+-", "context-cases", "v2", False, True),
    ("eval/context/cases-v3.json", "context_case", "id", r"^v\d+-", "context-cases", "v3", False, True),
    ("eval/learning/skill-cases-v2.json", "skill_case", "id", None, "skill-cases", "v2", False, True),
    ("eval/learning/v1.json", "learning_case", "id", None, "learning-v1", "v1", False, True),
    ("eval/memory/cases.json", "memory_case", "id", None, "memory-cases", "v1", False, True),
    ("eval/product/dev/tasks.json", "task_candidate", "task_id", None, "product-dev", "v1", True, False),
    ("eval/product/dev-v2/tasks.json", "task_candidate", "task_id", None, "product-dev", "v2", True, False),
]


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_rows(path: pathlib.Path) -> tuple[list[dict], str]:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif path.suffix == ".yaml":
        parsed = yaml.safe_load(text)
        rows = parsed["cases"] if isinstance(parsed, dict) else parsed
    else:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            rows = parsed
        else:
            rows = parsed.get("cases") or parsed.get("tasks") or parsed.get("items") or []
    if not isinstance(rows, list):
        raise SystemExit(f"{path}: unsupported asset shape")
    return rows, digest_bytes(raw)


def canonical_id(row: dict, id_field: str | None, normalization: str | None) -> str:
    if id_field is None:
        return digest_bytes(json.dumps(row, sort_keys=True, ensure_ascii=False).encode())
    value = str(row.get(id_field) or "")
    if normalization:
        value = re.sub(normalization, "", value)
    if not value:
        raise SystemExit(f"asset row has empty {id_field}: {row}")
    return value


def content_hash(row: dict) -> str:
    return digest_bytes(json.dumps(row, sort_keys=True, ensure_ascii=False).encode())


def fixture_resolvable(tasks: list[dict]) -> bool | None:
    hashes = {str(row.get("catalog_hash") or "") for row in tasks} - {""}
    if not hashes:
        return None
    repo_hashes = set()
    for pattern in ("data/*.jsonl", "eval/**/*.jsonl"):
        for path in ROOT.glob(pattern):
            repo_hashes.add(digest_bytes(path.read_bytes()))
    return hashes <= repo_hashes


def scan(out: pathlib.Path) -> dict:
    entries, families, seen_content = [], {}, {}
    inputs = []
    for rel, kind, id_field, norm, family, version, deterministic, prompt_dependent in ASSETS:
        path = ROOT / rel
        if not path.exists():
            entries.append({"path": rel, "status": "missing"})
            continue
        rows, file_hash = load_rows(path)
        inputs.append({"path": rel, "sha256": file_hash, "records": len(rows)})
        canonical, duplicates = {}, []
        for row in rows:
            cid = canonical_id(row, id_field, norm)
            if cid in canonical:
                duplicates.append(cid)
            canonical[cid] = row
            seen_content.setdefault(content_hash(row), []).append(rel)
        family_state = families.setdefault(family, {"versions": [], "effective_ids": set(),
                                                    "raw_total": 0, "latest": None})
        family_state["versions"].append({"version": version, "path": rel, "raw": len(rows)})
        family_state["raw_total"] += len(rows)
        family_state["effective_ids"].update(canonical)
        if family_state["latest"] is None or version >= family_state["latest"]:
            family_state["latest"] = version
        entry = {
            "path": rel, "kind": kind, "family": family, "version": version,
            "id_field": id_field, "id_normalization": norm,
            "raw_record_count": len(rows), "unique_id_count": len(canonical),
            "duplicate_ids": sorted(set(duplicates)),
            "sha256": file_hash,
            "deterministic": deterministic, "prompt_dependent": prompt_dependent,
            "status": "ok",
        }
        if kind == "task_candidate":
            entry["fixture_resolvable"] = fixture_resolvable(rows)
        entries.append(entry)

    family_rows = []
    for name, state in sorted(families.items()):
        family_rows.append({
            "family": name, "latest_version": state["latest"],
            "versions": state["versions"], "raw_total": state["raw_total"],
            "effective_case_count": len(state["effective_ids"]),
        })
    cross_file_duplicates = {h: sorted(set(v)) for h, v in seen_content.items() if len(set(v)) > 1}
    result = {
        "scan_version": SCAN_VERSION,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
        "assets": entries,
        "families": family_rows,
        "cross_file_duplicate_hashes": len(cross_file_duplicates),
        "cross_file_duplicate_examples": dict(list(cross_file_duplicates.items())[:5]),
        "totals": {
            "raw_records": sum(e.get("raw_record_count", 0) for e in entries),
            "effective_cases": sum(f["effective_case_count"] for f in family_rows),
        },
    }
    out.mkdir(parents=True, exist_ok=False)
    (out / "source-inventory.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    files = {p.name: digest_bytes(p.read_bytes()) for p in sorted(out.iterdir()) if p.is_file()}
    manifest = {"scan_version": SCAN_VERSION, "inputs": inputs, "outputs": files}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    result = scan(args.out.resolve())
    print(json.dumps({"totals": result["totals"], "families": len(result["families"]),
                      "cross_file_duplicates": result["cross_file_duplicate_hashes"]}, ensure_ascii=False))
    for row in result["families"]:
        print(f"{row['family']:22s} raw={row['raw_total']:4d} effective={row['effective_case_count']:4d} latest={row['latest_version']}")


if __name__ == "__main__":
    main()
