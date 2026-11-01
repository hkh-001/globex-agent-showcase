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


def test_required_fields_must_appear_in_observed_fields(tmp_path):
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    record = next(row for row in evidence["records"] if row["capability_id"] == "memory.remember")
    record["required_fields"] = ["action", "statement"]
    record["observed_fields"] = ["action"]
    evidence["records_sha256"] = __import__("hashlib").sha256(
        json.dumps(evidence["records"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    errors = capability_audit.audit(MATRIX, ROOT / "eval/agent/v1/unsupported-capabilities.json",
                                    ROOT / "eval/agent/v1/template-catalog.json", None, path)
    assert any("observed_fields miss" in error for error in errors)


def test_state_snapshot_without_values_fails(tmp_path):
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    record = next(row for row in evidence["records"]
                  if row["observation_source"] == "state_snapshot" and row["result"] == "pass")
    record["observed_values"] = {}
    evidence["records_sha256"] = __import__("hashlib").sha256(
        json.dumps(evidence["records"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    errors = capability_audit.audit(MATRIX, ROOT / "eval/agent/v1/unsupported-capabilities.json",
                                    ROOT / "eval/agent/v1/template-catalog.json", None, path)
    assert any("lacks observed_values" in error for error in errors)


def test_probe_nodeid_must_be_declared_in_matrix(tmp_path):
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    record = next(row for row in evidence["records"] if row["capability_id"] == "memory.remember")
    record["test_nodeid"] = "tests/test_agentbench_capability.py::test_capability_audit_passes_on_repo_files"
    evidence["records_sha256"] = __import__("hashlib").sha256(
        json.dumps(evidence["records"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    errors = capability_audit.audit(MATRIX, ROOT / "eval/agent/v1/unsupported-capabilities.json",
                                    ROOT / "eval/agent/v1/template-catalog.json", None, path)
    assert any("not declared in matrix" in error for error in errors)


def test_pass_record_without_values_fails(tmp_path):
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    record = next(row for row in evidence["records"]
                  if row["capability_id"] == "search.catalog" and row["result"] == "pass")
    record["observed_values"] = {}
    evidence["records_sha256"] = __import__("hashlib").sha256(
        json.dumps(evidence["records"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    errors = capability_audit.audit(MATRIX, ROOT / "eval/agent/v1/unsupported-capabilities.json",
                                    ROOT / "eval/agent/v1/template-catalog.json", None, path)
    assert any("has no observed_values" in error for error in errors)


def _tampered_evidence(tmp_path, mutate):
    import hashlib
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))
    mutate(evidence["records"])
    evidence["records_sha256"] = hashlib.sha256(
        json.dumps(evidence["records"], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence), encoding="utf-8")
    return capability_audit.audit(MATRIX, ROOT / "eval/agent/v1/unsupported-capabilities.json",
                                  ROOT / "eval/agent/v1/template-catalog.json", None, path)


def test_missing_value_assertion_fails(tmp_path):
    def mutate(records):
        record = next(row for row in records if row["capability_id"] == "search.catalog")
        record["value_assertions"] = record["value_assertions"][1:]
    errors = _tampered_evidence(tmp_path, mutate)
    assert any("has no value assertion" in error for error in errors)


def test_wrong_expected_value_fails(tmp_path):
    def mutate(records):
        record = next(row for row in records if row["capability_id"] == "search.exact_id")
        assertion = record["value_assertions"][0]
        assertion["operator"] = "equals"
        assertion["expected"] = "wrong"
    errors = _tampered_evidence(tmp_path, mutate)
    assert any("expected" in error for error in errors)


def test_wrong_projection_fails(tmp_path):
    def mutate(records):
        record = next(row for row in records if row["capability_id"] == "search.catalog")
        record["value_assertions"][0]["projection"] = "bad projection!"
    errors = _tampered_evidence(tmp_path, mutate)
    assert any("invalid projection" in error for error in errors)


def test_unrelated_dict_does_not_pass(tmp_path):
    def mutate(records):
        record = next(row for row in records if row["capability_id"] == "memory.update")
        record["value_assertions"] = [{"field": "unrelated", "projection": None,
                                       "operator": "equals", "actual": 1, "expected": 1}]
    errors = _tampered_evidence(tmp_path, mutate)
    assert any("unrelated assertion field" in error for error in errors)


def test_synchronized_actual_and_expected_tamper_fails(tmp_path):
    def mutate(records):
        record = next(row for row in records if row["capability_id"] == "search.catalog")
        assertion = next(item for item in record["value_assertions"] if item["field"] == "hit_count")
        assertion["actual"] = 999
        assertion["expected"] = 999
        assertion["operator"] = "equals"
    errors = _tampered_evidence(tmp_path, mutate)
    assert any("!= observed" in error for error in errors)


def test_missing_observed_source_fails(tmp_path):
    def mutate(records):
        record = next(row for row in records if row["capability_id"] == "search.catalog")
        del record["observed_values"]["hit_count"]
    errors = _tampered_evidence(tmp_path, mutate)
    assert any("no observed source value" in error for error in errors)


def test_wrong_capability_status_fails(tmp_path):
    def mutate(records):
        record = next(row for row in records
                      if row["capability_id"] == "trade.prepare" and row["observation_source"] == "event")
        record["observed_values"]["confirmation.status"] = "CANCELLED"
        for assertion in record["value_assertions"]:
            if assertion["field"] == "confirmation.status":
                assertion["actual"] = "CANCELLED"
                assertion["expected"] = "CANCELLED"
    errors = _tampered_evidence(tmp_path, mutate)
    assert any("capability contract" in error for error in errors)
