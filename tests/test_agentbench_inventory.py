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
    assert families["harness-cases"]["latest_version"] == "v1-cache"


def test_select_latest_uses_explicit_numeric_order():
    assert inventory.select_latest([("v2", 2), ("v10", 10)]) == "v10"
    assert inventory.select_latest([("v1", 1), ("v2", 2), ("v3", 3)]) == "v3"
    with pytest.raises(SystemExit):
        inventory.select_latest([("v2", 2), ("v3", 2)])


def test_product_fixture_is_unresolved_and_not_directly_reusable(tmp_path):
    result = _scan(tmp_path)
    entry = next(row for row in result["assets"] if row["path"] == "eval/product/dev/tasks.json")
    assert entry["fixture_resolution"] == "unresolved_by_raw_sha256"
    assert entry["task_reusability"] == "unresolved"
    assert entry["automatic_oracle_defined"] is True
    assert entry["automatic_oracle_executable"] is False
    assert entry["oracle_unavailable_reason"] == "unresolved_catalog_fixture"


def test_reusability_and_oracle_flags_are_kind_specific(tmp_path):
    result = _scan(tmp_path)
    entries = {row["path"]: row for row in result["assets"] if row.get("status") == "ok"}
    assert entries["eval/v2/product_retrieval.jsonl"]["task_reusability"] == "conversion_required"
    assert entries["eval/v2/product_retrieval.jsonl"]["automatic_oracle_defined"] is True
    assert entries["eval/v2/product_retrieval.jsonl"]["automatic_oracle_executable"] is True
    assert entries["eval/v3/semantic_paraphrases.jsonl"]["task_reusability"] == "oracle_only"
    assert entries["eval/cases.yaml"]["automatic_oracle_defined"] is False


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


def test_exact_hash_is_raw_and_normalized_hash_collapses_whitespace():
    exact_a, normalized_a, _ = inventory.prompt_hashes("预算  300\n元")
    exact_b, normalized_b, _ = inventory.prompt_hashes("预算 300 元")
    assert exact_a != exact_b
    assert normalized_a == normalized_b
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


def test_discovery_flags_new_case_schemas_and_ignores_configs(tmp_path):
    eval_dir = tmp_path / "eval"
    eval_dir.mkdir()
    (eval_dir / "messages.json").write_text(json.dumps({"cases": [{"messages": [{"role": "user"}]}]}))
    (eval_dir / "prompt.jsonl").write_text(json.dumps({"prompt": "hello"}) + "\n")
    (eval_dir / "manifest.json").write_text(json.dumps({"schema": "x"}))
    (eval_dir / "config.json").write_text(json.dumps({"threshold": 1}))
    discovery = inventory.discover(tmp_path)
    unclassified = {row["path"] for row in discovery["unclassified_candidate_assets"]}
    assert unclassified == {"eval/messages.json", "eval/prompt.jsonl"}
    ignored = {row["path"]: row["reason"] for row in discovery["ignored_with_reason"]}
    assert ignored["eval/manifest.json"] == "manifest_or_evidence"
    assert ignored["eval/config.json"] == "unsupported_schema"
    assert "eval/product_recall.jsonl" in discovery["missing_registered_assets"]


def test_record_index_covers_every_record_and_keeps_bindings(tmp_path):
    result = _scan(tmp_path)
    rows = [json.loads(line) for line in (tmp_path / "out" / "record-index.jsonl").read_text().splitlines() if line.strip()]
    assert len(rows) == result["record_count"]
    assert all(row["leakage_group_id"] for row in rows)
    paraphrases = [row for row in rows if row["asset_path"] == "eval/v3/semantic_paraphrases.jsonl"]
    assert len(paraphrases) == 72
    assert all(row["derived_from"] == "retrieval-v2" for row in paraphrases)
    assert all(row["reference_case_id"] for row in paraphrases)


def test_derived_paraphrases_bind_to_referenced_v2_group(tmp_path):
    result = _scan(tmp_path)
    entry = next(row for row in result["assets"] if row["path"] == "eval/v3/semantic_paraphrases.jsonl")
    assert entry["derived_from"] == "retrieval-v2"
    assert entry["unresolved_references"] == 0
    assert entry["leakage_group_count"] == 24


def test_scan_is_deterministic_across_output_directories(tmp_path):
    first = inventory.scan(tmp_path / "one")
    second = inventory.scan(tmp_path / "two")
    assert first["deterministic_payload_sha256"] == second["deterministic_payload_sha256"]


def test_manifest_hashes_can_be_recomputed(tmp_path):
    inventory.scan(tmp_path / "out")
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert "record-index.jsonl" in manifest["outputs"]
    for name, expected in manifest["outputs"].items():
        assert hashlib.sha256((tmp_path / "out" / name).read_bytes()).hexdigest() == expected


def test_multi_turn_structure_is_not_lost_by_normalization():
    first = inventory.prompt_hashes(["a b", "c"])
    second = inventory.prompt_hashes(["a", "b c"])
    assert first[1] != second[1]
    assert first[2] != second[2]


def test_deterministic_hash_binds_record_index(tmp_path, monkeypatch):
    baseline = inventory.scan(tmp_path / "base")
    original = inventory.prompt_groups

    def tampered(rows, spec):
        groups = original(rows, spec)
        if spec["path"] == "eval/context/cases-v3.json":
            groups[0]["leakage_group_id"] = "tampered-leakage-group"
        return groups

    monkeypatch.setattr(inventory, "prompt_groups", tampered)
    changed = inventory.scan(tmp_path / "changed")
    assert changed["record_index_sha256"] != baseline["record_index_sha256"]
    assert changed["deterministic_payload_sha256"] != baseline["deterministic_payload_sha256"]


def test_manifest_records_scanner_source_and_record_index(tmp_path):
    inventory.scan(tmp_path / "out")
    manifest = json.loads((tmp_path / "out" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["scanner_sha256"]
    assert manifest["record_index_sha256"]
    assert manifest["source_commit"]
    assert isinstance(manifest["tracked_worktree_clean"], bool)
