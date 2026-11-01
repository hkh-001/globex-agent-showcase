"""Freeze a minimal, sanitized inventory for deterministic catalog regeneration."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
from datetime import datetime, timezone

ROOT = pathlib.Path(__file__).resolve().parents[3]
ASSET_FIELDS = ["path", "family", "status", "version_order", "prompt_dependent",
                "automatic_oracle_executable", "task_reusability", "derived_from"]
RECORD_FIELDS = ["record_id", "asset_path", "family", "template_id", "leakage_group_id",
                 "exact_prompt_sha256", "normalized_prompt_sha256", "derived_from", "split", "version"]
TOP_FIELDS = ["scan_version", "prompt_normalization", "source_commit", "tracked_worktree_clean",
              "scanner_sha256", "record_index_sha256", "deterministic_payload_sha256", "totals"]
UNSAFE = [re.compile(r"/Users/"), re.compile(r"/home/"), re.compile(r"sk-[A-Za-z0-9]{8,}"),
          re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), re.compile(r"TAVILY_API_KEY")]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True,
                          check=True).stdout.strip()


def _unsafe(text: str) -> list[str]:
    return [pattern.pattern for pattern in UNSAFE if pattern.search(text)]


def freeze(inventory_dir: pathlib.Path, out_dir: pathlib.Path) -> dict:
    inventory = json.loads((inventory_dir / "source-inventory.json").read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in
            (inventory_dir / "record-index.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    frozen_inventory = {key: inventory[key] for key in TOP_FIELDS if key in inventory}
    frozen_inventory["assets"] = [{key: asset.get(key) for key in ASSET_FIELDS} for asset in inventory["assets"]]
    frozen_rows = [{key: row.get(key) for key in RECORD_FIELDS if key in row} for row in rows]
    out_dir.mkdir(parents=True, exist_ok=True)
    inventory_text = json.dumps(frozen_inventory, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    record_text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in frozen_rows)
    for name, text in (("source-inventory.json", inventory_text), ("record-index.jsonl", record_text)):
        unsafe = _unsafe(text)
        if unsafe:
            raise SystemExit(f"frozen {name} contains unsafe patterns: {unsafe}")
    (out_dir / "source-inventory.json").write_text(inventory_text, encoding="utf-8")
    (out_dir / "record-index.jsonl").write_text(record_text, encoding="utf-8")
    manifest = {
        "schema_version": "agentbench-frozen-inventory-v1",
        "source_commit": inventory.get("source_commit"),
        "source_tree": _git("rev-parse", "HEAD^{tree}"),
        "tracked_worktree_clean": True,
        "scanner_sha256": inventory.get("scanner_sha256"),
        "generator_sha256": _sha((ROOT / "scripts/eval/agentbench/template_catalog.py").read_bytes()),
        "inventory_file_sha256": _sha(inventory_text.encode()),
        "record_index_file_sha256": _sha(record_text.encode()),
        "counts": {
            "raw_records": inventory.get("totals", {}).get("raw_records"),
            "family_effective_cases": inventory.get("totals", {}).get("family_effective_cases"),
            "records": len(frozen_rows),
            "assets": len(frozen_inventory["assets"]),
        },
        "prompt_normalization": inventory.get("prompt_normalization"),
        "record_semantics": "2131 asset records, not 2131 executable agent tasks",
        "command": "python -m scripts.eval.agentbench.freeze_inventory --inventory <evidence-dir> --out eval/agent/v1/inventory",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, default=ROOT / "eval/agent/v1/inventory")
    args = parser.parse_args()
    manifest = freeze(args.inventory, args.out)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
