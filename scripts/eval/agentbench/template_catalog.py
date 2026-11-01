"""Generate the AgentBench template catalog from an inventory evidence directory.

One entry per leakage group, so Stage 2 can keep groups atomic across
Dev/Release/Holdout.  Multi-source groups use conservative aggregation: any
prompt dependency wins, oracle executability requires all sources, reusability
takes the strictest status, and a module conflict aborts generation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib

MODULE_BY_FAMILY = {
    "retrieval-legacy": "search",
    "retrieval-v1": "search",
    "retrieval-v2": "search",
    "retrieval-v3": "search",
    "retrieval-v3-paraphrase": "search",
    "retrieval-category": "search",
    "retrieval-optimization": "search",
    "optimization-contract": "search",
    "knowledge-v1": "knowledge",
    "business-public": "transaction",
    "model-eval": "conversation",
    "agent-cases-v1": "routing",
    "harness-cases": "context",
    "context-cases": "context",
    "skill-cases": "memory",
    "learning-v1": "memory",
    "memory-cases": "memory",
    "product-dev": "transaction",
}
REUSABILITY_ORDER = ["direct", "conversion_required", "oracle_only", "unresolved"]


def _stable_hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def bucket(group_id: str) -> str:
    value = int(hashlib.sha256(group_id.encode()).hexdigest()[:8], 16) % 100
    return "dev" if value < 50 else ("release" if value < 82 else "holdout")


def _family_conversions(family: str) -> list[str]:
    conversions = []
    if family.startswith("retrieval") or family == "knowledge-v1":
        conversions.append("wrap-as-agent-task")
    if family in {"harness-cases", "context-cases"}:
        conversions.append("extract-execution-contract")
    if family == "product-dev":
        conversions.append("resolve-catalog-fixture")
    return conversions


def build(inventory_dir: pathlib.Path) -> dict:
    inventory = json.loads((inventory_dir / "source-inventory.json").read_text(encoding="utf-8"))
    records = [json.loads(line) for line in
               (inventory_dir / "record-index.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    assets_by_family = {}
    for asset in inventory["assets"]:
        if asset.get("status") != "ok":
            continue
        current = assets_by_family.get(asset["family"])
        if current is None or asset["version_order"] > current["version_order"]:
            assets_by_family[asset["family"]] = asset

    groups = {}
    for row in records:
        group = groups.setdefault(row["leakage_group_id"], {
            "record_count": 0, "assets": set(), "families": set(),
            "template_ids": set(), "split": bucket(row["leakage_group_id"]),
        })
        group["record_count"] += 1
        group["assets"].add(row["asset_path"])
        group["families"].add(row["family"])
        if row["template_id"]:
            group["template_ids"].add(row["template_id"])

    templates = []
    for group_id, group in groups.items():
        modules = {MODULE_BY_FAMILY.get(family) for family in group["families"]}
        if None in modules:
            raise SystemExit(f"{group_id}: unmapped family {sorted(group['families'])}")
        if len(modules) != 1:
            raise SystemExit(f"{group_id}: module conflict {sorted(modules)}")
        assets = [assets_by_family[family] for family in sorted(group["families"])
                  if family in assets_by_family]
        prompt_dependent = any(asset.get("prompt_dependent") for asset in assets)
        oracle_executable = all(asset.get("automatic_oracle_executable", True) for asset in assets)
        reusability = max((asset.get("task_reusability", "unresolved") for asset in assets),
                          key=REUSABILITY_ORDER.index)
        conversions = set()
        for asset in assets:
            conversions.update(_family_conversions(asset["family"]))
            if asset.get("prompt_dependent"):
                conversions.add("prompt-adaptation")
            if not asset.get("automatic_oracle_executable", True):
                conversions.add("fixture-required")
        derived = sorted(asset["path"] for asset in assets if asset.get("derived_from"))
        primary_candidates = [asset["path"] for asset in assets if not asset.get("derived_from")]
        templates.append({
            "leakage_group_id": group_id,
            "module": modules.pop(),
            "source_assets": sorted(group["assets"]),
            "families": sorted(group["families"]),
            "template_ids": sorted(group["template_ids"]),
            "record_count": group["record_count"],
            "prompt_dependent": prompt_dependent,
            "automatic_oracle_executable": oracle_executable,
            "task_reusability": reusability,
            "conversion_requirements": sorted(conversions),
            "primary_source": primary_candidates[0] if primary_candidates else None,
            "derived_sources": derived,
            "source_flags": {family: bool(assets_by_family[family].get("prompt_dependent"))
                             for family in sorted(group["families"]) if family in assets_by_family},
            "recommended_split": group["split"],
        })
    templates.sort(key=lambda item: item["leakage_group_id"])
    split_counts = {"dev": 0, "release": 0, "holdout": 0}
    module_splits = {}
    for template in templates:
        split_counts[template["recommended_split"]] += 1
        module_splits.setdefault(template["module"], {"dev": 0, "release": 0, "holdout": 0})
        module_splits[template["module"]][template["recommended_split"]] += 1

    generator = pathlib.Path(__file__).resolve()
    catalog = {
        "schema_version": "agentbench-template-catalog-v1.1",
        "inventory_source_commit": inventory.get("source_commit"),
        "inventory_record_index_sha256": inventory.get("record_index_sha256"),
        "inventory_deterministic_payload_sha256": inventory.get("deterministic_payload_sha256"),
        "generator_sha256": hashlib.sha256(generator.read_bytes()).hexdigest(),
        "split_policy": "sha256(leakage_group_id) mod 100: <50 dev, <82 release, else holdout; groups stay atomic",
        "holdout_scope": "Holdout supports aggregate conclusions only; module-level rates are not reportable",
        "group_count": len(templates),
        "record_count": sum(item["record_count"] for item in templates),
        "recommended_split_counts": split_counts,
        "module_split_distribution": module_splits,
        "templates": templates,
    }
    catalog["catalog_sha256"] = _stable_hash(catalog)
    return catalog


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    catalog = build(args.inventory)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"groups": catalog["group_count"], "records": catalog["record_count"],
                      "splits": catalog["recommended_split_counts"],
                      "catalog_sha256": catalog["catalog_sha256"][:16]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
