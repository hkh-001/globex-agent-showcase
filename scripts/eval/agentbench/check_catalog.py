"""CI gate: rebuild the template catalog from the frozen inventory and compare."""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[3]
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(*args: str) -> tuple[int, str]:
    process = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True)
    return process.returncode, process.stdout


def _git_blob(commit: str, path_text: str) -> bytes | None:
    process = subprocess.run(["git", "show", f"{commit}:{path_text}"], cwd=ROOT,
                             capture_output=True)
    return process.stdout if process.returncode == 0 else None


def _verify_commit_tree(commit: str, tree: str, label: str) -> list[str]:
    errors = []
    code, _ = _git("cat-file", "-e", f"{commit}^{{commit}}")
    if code != 0:
        return [f"{label} commit {commit} does not exist"]
    code, _ = _git("merge-base", "--is-ancestor", commit, "HEAD")
    if code != 0:
        errors.append(f"{label} commit {commit} is not an ancestor of HEAD")
    code, actual = _git("rev-parse", f"{commit}^{{tree}}")
    if code != 0 or actual.strip() != tree:
        errors.append(f"{label} tree {tree} does not match commit {commit}")
    return errors


def check(frozen_dir: pathlib.Path, catalog_path: pathlib.Path,
          out_path: pathlib.Path | None = None) -> list[str]:
    errors: list[str] = []
    manifest = json.loads((frozen_dir / "manifest.json").read_text(encoding="utf-8"))
    frozen_inventory = json.loads((frozen_dir / "source-inventory.json").read_text(encoding="utf-8"))
    rows = [line for line in (frozen_dir / "record-index.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()]

    if manifest.get("tracked_worktree_clean") is not True:
        errors.append("manifest says the freeze was not generated from a clean tree")
    if manifest.get("inventory_file_sha256") != _sha(frozen_dir / "source-inventory.json"):
        errors.append("frozen source-inventory hash mismatch")
    if manifest.get("record_index_file_sha256") != _sha(frozen_dir / "record-index.jsonl"):
        errors.append("frozen record-index hash mismatch")

    errors.extend(_verify_commit_tree(manifest.get("inventory_source_commit", ""),
                                      manifest.get("inventory_source_tree", ""), "inventory source"))
    errors.extend(_verify_commit_tree(manifest.get("freeze_implementation_commit", ""),
                                      manifest.get("freeze_implementation_tree", ""), "freeze implementation"))

    inventory_commit = manifest.get("inventory_source_commit", "")
    scanner_blob = _git_blob(inventory_commit, "scripts/eval/agentbench/inventory.py")
    if scanner_blob is None:
        errors.append("cannot read inventory.py at inventory source commit")
    elif hashlib.sha256(scanner_blob).hexdigest() != manifest.get("scanner_sha256"):
        errors.append("scanner_sha256 does not match inventory.py at inventory source commit")
    if manifest.get("scanner_sha256") != frozen_inventory.get("scanner_sha256"):
        errors.append("scanner_sha256 does not match the frozen inventory")

    generator_blob = _git_blob(manifest.get("freeze_implementation_commit", ""),
                               "scripts/eval/agentbench/template_catalog.py")
    if generator_blob is None:
        errors.append("cannot read template_catalog.py at freeze implementation commit")
    elif hashlib.sha256(generator_blob).hexdigest() != manifest.get("generator_sha256"):
        errors.append("generator_sha256 does not match template_catalog.py at freeze commit")
    if _sha(ROOT / "scripts/eval/agentbench/template_catalog.py") != manifest.get("generator_sha256"):
        errors.append("generator hash mismatch with the current tree")

    counts = manifest.get("counts") or {}
    totals = frozen_inventory.get("totals") or {}
    if counts.get("raw_records") != totals.get("raw_records"):
        errors.append("manifest raw_records does not match frozen inventory")
    if counts.get("family_effective_cases") != totals.get("family_effective_cases"):
        errors.append("manifest family_effective_cases does not match frozen inventory")
    if counts.get("records") != len(rows):
        errors.append("manifest record count does not match frozen record-index")
    if counts.get("assets") != len(frozen_inventory.get("assets") or []):
        errors.append("manifest asset count does not match frozen inventory")

    normalization = frozen_inventory.get("prompt_normalization")
    if manifest.get("prompt_normalization") != normalization:
        errors.append("prompt_normalization does not match the frozen inventory")
    if not isinstance(normalization, str) or not normalization:
        errors.append("prompt_normalization is missing")

    for key in ("record_index_sha256", "deterministic_payload_sha256"):
        value = frozen_inventory.get(key)
        if not isinstance(value, str) or not HEX64.match(value):
            errors.append(f"{key} is not a sha256 in the frozen inventory")

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
    if committed.get("inventory_record_index_sha256") != frozen_inventory.get("record_index_sha256"):
        errors.append("catalog record-index hash does not match frozen inventory")
    if committed.get("inventory_deterministic_payload_sha256") != frozen_inventory.get(
            "deterministic_payload_sha256"):
        errors.append("catalog deterministic hash does not match frozen inventory")
    if committed.get("record_count") != len(rows):
        errors.append("catalog record count does not match frozen record-index")

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
