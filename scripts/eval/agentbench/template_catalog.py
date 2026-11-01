"""Generate the AgentBench template catalog from an inventory evidence directory.

One entry per leakage group, so Stage 2 can keep groups atomic across
Dev/Release/Holdout.  The split is a deterministic hash bucket; it is a
*recommendation*, not a committed split.
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


def bucket(group_id: str) -> str:
    value = int(hashlib.sha256(group_id.encode()).hexdigest()[:8], 16) % 100
    return "dev" if value < 50 else ("release" if value < 82 else "holdout")


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
            "leakage_group_id": row["leakage_group_id"],
            "record_count": 0, "assets": set(), "families": set(),
            "template_ids": set(), "split": bucket(row["leakage_group_id"]),
        })
        group["record_count"] += 1
        group["assets"].add(row["asset_path"])
        group["families"].add(row["family"])
        if row["template_id"]:
            group["template_ids"].add(row["template_id"])

    templates = []
    for group in groups.values():
        family = sorted(group["families"])[0]
        asset = assets_by_family.get(family, {})
        conversions = []
        if asset.get("prompt_dependent"):
            conversions.append("prompt-adaptation")
        if not asset.get("automatic_oracle_executable", True):
            conversions.append("fixture-required")
        if family.startswith("retrieval") or family == "knowledge-v1":
            conversions.append("wrap-as-agent-task")
        if family in {"harness-cases", "context-cases"}:
            conversions.append("extract-execution-contract")
        if family == "product-dev":
            conversions.append("resolve-catalog-fixture")
        templates.append({
            "leakage_group_id": group["leakage_group_id"],
            "module": MODULE_BY_FAMILY.get(family, "unclassified"),
            "source_assets": sorted(group["assets"]),
            "families": sorted(group["families"]),
            "template_ids": sorted(group["template_ids"]),
            "record_count": group["record_count"],
            "prompt_dependent": bool(asset.get("prompt_dependent")),
            "automatic_oracle_executable": bool(asset.get("automatic_oracle_executable", True)),
            "conversion_requirements": conversions,
            "recommended_split": group["split"],
        })
    templates.sort(key=lambda item: item["leakage_group_id"])
    counts = {"dev": 0, "release": 0, "holdout": 0}
    for template in templates:
        counts[template["recommended_split"]] += 1
    return {
        "schema_version": "agentbench-template-catalog-v1",
        "inventory_source_commit": inventory.get("source_commit"),
        "inventory_record_index_sha256": inventory.get("record_index_sha256"),
        "split_policy": "sha256(leakage_group_id) mod 100: <50 dev, <82 release, else holdout; groups stay atomic",
        "group_count": len(templates),
        "record_count": sum(item["record_count"] for item in templates),
        "recommended_split_counts": counts,
        "templates": templates,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", type=pathlib.Path, required=True)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    catalog = build(args.inventory)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"groups": catalog["group_count"], "records": catalog["record_count"],
                      "splits": catalog["recommended_split_counts"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
