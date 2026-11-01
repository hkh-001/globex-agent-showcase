"""Contract tests for the capability matrix, unsupported list and template catalog."""
from __future__ import annotations

import copy
import json
import pathlib

from scripts.eval.agentbench import capability_audit

ROOT = pathlib.Path(__file__).resolve().parents[1]
MATRIX = ROOT / "eval/agent/v1/capability-matrix.json"
UNSUPPORTED = ROOT / "eval/agent/v1/unsupported-capabilities.json"
CATALOG = ROOT / "eval/agent/v1/template-catalog.json"


def test_capability_audit_passes_on_repo_files():
    assert capability_audit.audit(MATRIX, UNSUPPORTED, CATALOG) == []


def test_supported_capability_without_fields_fails(tmp_path):
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    target = next(item for item in matrix["capabilities"] if item["status"] == "supported")
    target.pop("assertable_fields")
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(matrix), encoding="utf-8")
    errors = capability_audit.audit(path, UNSUPPORTED, CATALOG)
    assert any("missing assertable_fields" in error for error in errors)


def test_supported_capability_needing_instrumentation_fails(tmp_path):
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    target = next(item for item in matrix["capabilities"] if item["status"] == "supported")
    target["needs_new_instrumentation"] = True
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(matrix), encoding="utf-8")
    errors = capability_audit.audit(path, UNSUPPORTED, CATALOG)
    assert any("cannot need new instrumentation" in error for error in errors)


def test_unsupported_entries_carry_reason_and_evidence():
    unsupported = json.loads(UNSUPPORTED.read_text(encoding="utf-8"))["unsupported"]
    assert unsupported
    assert all(item.get("reason") and item.get("evidence") for item in unsupported)


def test_template_catalog_is_group_atomic_and_classified():
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    templates = catalog["templates"]
    assert catalog["group_count"] == len(templates)
    assert catalog["record_count"] == sum(item["record_count"] for item in templates)
    assert all(item["recommended_split"] in {"dev", "release", "holdout"} for item in templates)
    assert all(item["module"] != "unclassified" for item in templates)
    counts = {"dev": 0, "release": 0, "holdout": 0}
    for item in templates:
        counts[item["recommended_split"]] += 1
    assert counts == catalog["recommended_split_counts"]
