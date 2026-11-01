"""Contract tests for the Stage 1 asset inventory scanner."""
from __future__ import annotations

import hashlib
import json

import pytest

from scripts.eval.agentbench import inventory


def _scan(tmp_path, name="out"):
    return inventory.scan(tmp_path / name)


def test_family_effective_counts_and_version_order(tmp_path):
    result = _scan(tmp_path)
    families = {row["family"]: row for row in result["families"]}
    assert families["context-cases"]["family_effective_cases"] == 40
    assert families["harness-cases"]["family_effective_cases"] == 54
    assert families["product-dev"]["family_effective_cases"] == 12
    # 版本顺序必须按显式 order，而不是字符串比较（v10 不能小于 v2）
    assert families["harness-cases"]["latest_version"] == "v1-cache"
    assert all(isinstance(spec["version_order"], int) for spec in inventory.SPECS)


def test_product_fixture_is_unresolved_by_raw_sha(tmp_path):
    result = _scan(tmp_path)
    entry = next(row for row in result["assets"] if row["path"] == "eval/product/dev/tasks.json")
    assert entry["fixture_resolution"] == "unresolved_by_raw_sha256"
    assert entry["directly_reusable"] is False


def test_cross_family_overlap_shares_leakage_group():
    context = next(spec for spec in inventory.SPECS
                   if spec["family"] == "context-cases" and spec["version"] == "v3")
    harness = next(spec for spec in inventory.SPECS
                   if spec["family"] == "harness-cases" and spec["version"] == "v1-base")
    context_rows, _ = inventory.load_rows(inventory.ROOT / context["path"])
    harness_rows, _ = inventory.load_rows(inventory.ROOT / harness["path"])
    context_groups = {row["duplicate_group_id"]: row for row in inventory.prompt_groups(context_rows, context)}
    harness_groups = {row["duplicate_group_id"]: row for row in inventory.prompt_groups(harness_rows, harness)}
    shared = set(context_groups) & set(harness_groups)
    assert len(shared) == 23
    for key in shared:
        assert context_groups[key]["leakage_group_id"] == harness_groups[key]["leakage_group_id"]


def test_prompt_normalization_does_not_strip_version_prefix():
    assert inventory.normalize_prompt("  v2   预算\n300 ") == "v2 预算 300"


def test_empty_id_fails_closed():
    spec = {"path": "x", "id_field": "id", "id_normalization": None}
    with pytest.raises(SystemExit):
        inventory.canonical_id({"id": ""}, spec)


def test_discovery_has_no_unclassified_or_missing_assets(tmp_path):
    result = _scan(tmp_path)
    discovery = result["discovery"]
    assert discovery["unclassified_candidate_assets"] == []
    assert discovery["missing_registered_assets"] == []
    known = {code for _, code in inventory.IGNORE_RULES} | {"unsupported_schema"}
    assert discovery["ignored_with_reason"]
    assert all(row["reason"] in known for row in discovery["ignored_with_reason"])


def test_scan_is_deterministic_across_output_directories(tmp_path):
    first = inventory.scan(tmp_path / "one")
    second = inventory.scan(tmp_path / "two")
    assert first["deterministic_payload_sha256"] == second["deterministic_payload_sha256"]


def test_manifest_hashes_can_be_recomputed(tmp_path):
    inventory.scan(tmp_path / "out")
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    for name, expected in manifest["outputs"].items():
        assert hashlib.sha256((tmp_path / "out" / name).read_bytes()).hexdigest() == expected


def test_derived_paraphrases_bind_to_referenced_v2_group(tmp_path):
    result = _scan(tmp_path)
    entry = next(row for row in result["assets"] if row["path"] == "eval/v3/semantic_paraphrases.jsonl")
    assert entry["derived_from"] == "retrieval-v2"
    assert entry["unresolved_references"] == 0
    assert entry["leakage_group_count"] == 24
