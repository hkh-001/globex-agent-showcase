"""Tests for the frozen inventory and the catalog CI gate."""
from __future__ import annotations

import json
import pathlib

import hashlib
import subprocess

import pytest

from scripts.eval.agentbench import check_catalog, freeze_inventory, template_catalog

ROOT = pathlib.Path(__file__).resolve().parents[1]
FROZEN = ROOT / "eval/agent/v1/inventory"
CATALOG = ROOT / "eval/agent/v1/template-catalog.json"


def test_frozen_catalog_rebuild_passes():
    assert check_catalog.check(FROZEN, CATALOG) == []


def test_rebuild_is_deterministic():
    first = template_catalog.build(FROZEN)
    second = template_catalog.build(FROZEN)
    assert first["catalog_sha256"] == second["catalog_sha256"]
    assert first == second


def test_manifest_counts_match_frozen_files():
    manifest = json.loads((FROZEN / "manifest.json").read_text(encoding="utf-8"))
    rows = [line for line in (FROZEN / "record-index.jsonl").read_text(encoding="utf-8").splitlines() if line]
    assets = json.loads((FROZEN / "source-inventory.json").read_text(encoding="utf-8"))["assets"]
    assert manifest["counts"]["records"] == len(rows)
    assert manifest["counts"]["assets"] == len(assets)
    assert manifest["counts"]["records"] == 2131


def test_tampered_record_index_fails(tmp_path):
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    for name in ("source-inventory.json", "record-index.jsonl", "manifest.json"):
        (frozen / name).write_bytes((FROZEN / name).read_bytes())
    with (frozen / "record-index.jsonl").open("a", encoding="utf-8") as stream:
        stream.write('{"record_id": "tampered"}\n')
    errors = check_catalog.check(frozen, CATALOG)
    assert any("record-index hash mismatch" in error for error in errors)


def test_tampered_catalog_fails(tmp_path):
    catalog = tmp_path / "catalog.json"
    data = json.loads(CATALOG.read_text(encoding="utf-8"))
    data["record_count"] = 1
    catalog.write_text(json.dumps(data), encoding="utf-8")
    errors = check_catalog.check(FROZEN, catalog)
    assert any("does not match" in error for error in errors)


def test_frozen_inputs_have_no_absolute_paths_or_emails():
    text = (FROZEN / "source-inventory.json").read_text(encoding="utf-8")
    text += (FROZEN / "record-index.jsonl").read_text(encoding="utf-8")
    assert "/Users/" not in text and "/home/" not in text
    assert "@" not in text


def test_manifest_commits_and_trees_resolve():
    manifest = json.loads((FROZEN / "manifest.json").read_text(encoding="utf-8"))
    for prefix in ("inventory_source", "freeze_implementation"):
        commit = manifest[f"{prefix}_commit"]
        tree = subprocess.run(["git", "rev-parse", f"{commit}^{{tree}}"], cwd=ROOT,
                              text=True, capture_output=True, check=True).stdout.strip()
        assert tree == manifest[f"{prefix}_tree"]
    assert manifest["inventory_source_commit"] != manifest["freeze_implementation_commit"]


def test_dirty_freeze_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(freeze_inventory, "is_clean", lambda: False)
    with pytest.raises(SystemExit):
        freeze_inventory.freeze(FROZEN, tmp_path / "out")


def test_coordinated_tamper_still_fails(tmp_path):
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    for name in ("source-inventory.json", "record-index.jsonl", "manifest.json"):
        (frozen / name).write_bytes((FROZEN / name).read_bytes())
    data = json.loads((frozen / "source-inventory.json").read_text(encoding="utf-8"))
    data["scanner_sha256"] = "0" * 64
    (frozen / "source-inventory.json").write_text(json.dumps(data), encoding="utf-8")
    manifest = json.loads((frozen / "manifest.json").read_text(encoding="utf-8"))
    manifest["scanner_sha256"] = "0" * 64
    manifest["inventory_file_sha256"] = hashlib.sha256(
        (frozen / "source-inventory.json").read_bytes()).hexdigest()
    (frozen / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    errors = check_catalog.check(frozen, CATALOG)
    assert any("scanner_sha256 does not match inventory.py" in error for error in errors)
