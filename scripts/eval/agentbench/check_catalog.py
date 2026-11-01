"""CI gate: rebuild the template catalog from the frozen inventory and compare."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check(frozen_dir: pathlib.Path, catalog_path: pathlib.Path,
          out_path: pathlib.Path | None = None) -> list[str]:
    errors = []
    manifest = json.loads((frozen_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("tracked_worktree_clean") is not True:
        errors.append("manifest says the freeze was not generated from a clean tree")
    if manifest.get("inventory_file_sha256") != _sha(frozen_dir / "source-inventory.json"):
        errors.append("frozen source-inventory hash mismatch")
    if manifest.get("record_index_file_sha256") != _sha(frozen_dir / "record-index.jsonl"):
        errors.append("frozen record-index hash mismatch")
    generator = ROOT / "scripts/eval/agentbench/template_catalog.py"
    if manifest.get("generator_sha256") != _sha(generator):
        errors.append("generator hash mismatch")
    if manifest.get("scanner_sha256") != json.loads(
            (frozen_dir / "source-inventory.json").read_text(encoding="utf-8")).get("scanner_sha256"):
        errors.append("scanner hash does not match the frozen inventory")
    from scripts.eval.agentbench import template_catalog
    try:
        regenerated = template_catalog.build(frozen_dir)
    except Exception as error:  # tampered inputs must fail closed, not crash
        errors.append(f"catalog regeneration failed: {error}")
        return errors
    committed = json.loads(catalog_path.read_text(encoding="utf-8"))
    if regenerated.get("catalog_sha256") != committed.get("catalog_sha256"):
        errors.append("regenerated catalog_sha256 does not match the committed catalog")
    if regenerated != committed:
        errors.append("regenerated catalog does not match the committed catalog")
    expected = manifest.get("counts", {})
    if expected.get("records") != regenerated.get("record_count"):
        errors.append("manifest record count does not match catalog")
    if expected.get("assets") != len(json.loads(
            (frozen_dir / "source-inventory.json").read_text(encoding="utf-8"))["assets"]):
        errors.append("manifest asset count does not match frozen inventory")
    if out_path is not None:
        out_path.write_text(json.dumps(regenerated, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--frozen", type=pathlib.Path, default=ROOT / "eval/agent/v1/inventory")
    parser.add_argument("--catalog", type=pathlib.Path,
                        default=ROOT / "eval/agent/v1/template-catalog.json")
    parser.add_argument("--out", type=pathlib.Path, default=None)
    args = parser.parse_args()
    errors = check(args.frozen, args.catalog, args.out)
    print(json.dumps({"passed": not errors, "errors": errors}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
