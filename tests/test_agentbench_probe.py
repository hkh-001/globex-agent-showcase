"""Negative and consistency tests for the runtime evidence probes."""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

from scripts.eval.agentbench import capability_audit, probes

ROOT = pathlib.Path(__file__).resolve().parents[1]
MATRIX = ROOT / "eval/agent/v1/capability-matrix.json"
EVIDENCE = ROOT / "eval/agent/v1/evidence-probe.json"


def test_missing_field_fails():
    records = [{"type": "tool.result", "payload": {"tool": "x", "present": 1}}]
    ok, observed = probes.check_fields(records, "tool.result:x", ["present", "missing"])
    assert ok is False
    assert "missing" not in observed


def test_wrong_selector_fails():
    records = [{"type": "tool.result", "payload": {"tool": "x", "present": 1}}]
    ok, _ = probes.check_fields(records, "tool.result:y", ["present"])
    assert ok is False


def test_wrong_event_type_fails():
    records = [{"type": "tool.invoke", "payload": {"tool": "x", "present": 1}}]
    ok, _ = probes.check_fields(records, "tool.result:x", ["present"])
    assert ok is False


def test_evidence_is_zero_paid_and_all_passed():
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    assert evidence["paid_requests"] == 0
    assert evidence["external_model_requests"] == 0
    assert all(row["result"] == "pass" for row in evidence["records"])


def test_verified_capabilities_match_probe_capabilities():
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    verified = {item["capability_id"] for item in matrix["capabilities"]
                if item.get("verification_status") == "verified"}
    assert verified == set(evidence["capabilities"])


def test_probe_records_reference_existing_tests():
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    for row in evidence["records"]:
        path_text, test_name = row["test_nodeid"].split("::", 1)
        text = (ROOT / path_text).read_text(encoding="utf-8")
        assert f"def {test_name}(" in text


def test_probe_cli_entrypoint_runs():
    process = subprocess.run([sys.executable, "-m", "scripts.eval.agentbench.probes"],
                             cwd=ROOT, text=True, capture_output=True)
    assert process.returncode == 0, process.stderr[-500:]


def test_tampered_records_fail_integrity_check(tmp_path):
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    evidence["records"][0]["result"] = "fail"
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    errors = capability_audit.audit(MATRIX, ROOT / "eval/agent/v1/unsupported-capabilities.json",
                                    ROOT / "eval/agent/v1/template-catalog.json", None, path)
    assert any("records_sha256" in error for error in errors)


def test_tampered_source_commit_fails_integrity_check(tmp_path):
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    evidence["source_commit"] = "0" * 40
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    errors = capability_audit.audit(MATRIX, ROOT / "eval/agent/v1/unsupported-capabilities.json",
                                    ROOT / "eval/agent/v1/template-catalog.json", None, path)
    assert any("does not exist" in error for error in errors)


def test_tampered_scanner_hash_fails_integrity_check(tmp_path):
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    evidence["scanner_sha256"] = "0" * 64
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    errors = capability_audit.audit(MATRIX, ROOT / "eval/agent/v1/unsupported-capabilities.json",
                                    ROOT / "eval/agent/v1/template-catalog.json", None, path)
    assert any("scanner_sha256" in error for error in errors)
