"""Validate the AgentBench capability matrix, unsupported list and template catalog.

v1.1: observations are source-typed, code entries are resolved by AST symbol,
supported capabilities must cite a real test that mentions their evidence, and
the catalog must be regenerable from the frozen inventory.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
SOURCE_TYPES = {"event", "tool_result", "verified_result", "state_snapshot", "private_artifact"}
STATUSES = {"supported", "instrumentation_required", "not_supported"}
VERIFICATION = {"verified", "failed", "pending"}
ORACLES = {"deterministic", "human", "none"}
REMEDIATION = {"instrumentation", "eval_harness", "dataset", "configuration",
               "product_feature", "external_data"}


def _module_symbols(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()

    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = f"{prefix}{child.name}"
                found.add(name)
                walk(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                name = f"{prefix}{child.name}"
                found.add(name)
                walk(child, name + ".")

    walk(tree, "")
    return found


def _test_exists(nodeid: str) -> bool:
    parts = nodeid.split("::")
    if len(parts) not in (2, 3):
        return False
    path = ROOT / parts[0]
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if len(parts) == 3:
        class_name, test_name = parts[1], parts[2]
        return f"class {class_name}" in text and bool(
            re.search(rf"(async\s+)?def\s+{re.escape(test_name)}\s*\(", text))
    return bool(re.search(rf"(async\s+)?def\s+{re.escape(parts[1])}\s*\(", text))


def _test_mentions(nodeid: str, tokens: set[str]) -> bool:
    path = ROOT / nodeid.split("::", 1)[0]
    text = path.read_text(encoding="utf-8")
    return any(token and token in text for token in tokens)


def _observation_errors(cid: str, observations) -> list[str]:
    errors = []
    if not isinstance(observations, list) or not observations:
        return [f"{cid}: observations must be a non-empty list"]
    for observation in observations:
        source = observation.get("source_type")
        if source not in SOURCE_TYPES:
            errors.append(f"{cid}: invalid source_type {source!r}")
        if not observation.get("selector"):
            errors.append(f"{cid}: observation selector is empty")
        fields = observation.get("fields")
        if not isinstance(fields, list) or not fields:
            errors.append(f"{cid}: observation fields are empty")
    return errors


def audit(matrix_path: pathlib.Path, unsupported_path: pathlib.Path,
          catalog_path: pathlib.Path | None = None,
          inventory_dir: pathlib.Path | None = None,
          probe_path: pathlib.Path | None = None) -> list[str]:
    errors: list[str] = []
    matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
    capabilities = matrix.get("capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        return ["matrix has no capabilities"]
    seen = set()
    status_by_id = {}
    for item in capabilities:
        cid = item.get("capability_id", "<missing>")
        if cid in seen:
            errors.append(f"duplicate capability_id {cid}")
        seen.add(cid)
        status_by_id[cid] = item.get("status")
        for key in ("capability_id", "code_entry", "observations", "allowed_paths",
                    "side_effects", "oracle_type", "status", "needs_new_instrumentation",
                    "verification_status", "evidence_test_nodeids", "candidate_asset_families"):
            if key not in item:
                errors.append(f"{cid}: missing {key}")
        if item.get("status") not in STATUSES:
            errors.append(f"{cid}: invalid status")
        if item.get("verification_status") not in VERIFICATION:
            errors.append(f"{cid}: invalid verification_status")
        if item.get("oracle_type") not in ORACLES:
            errors.append(f"{cid}: invalid oracle_type")
        if not isinstance(item.get("needs_new_instrumentation"), bool):
            errors.append(f"{cid}: needs_new_instrumentation must be bool")
        entry = item.get("code_entry") or {}
        if not isinstance(entry, dict) or not entry.get("path") or not entry.get("symbol"):
            errors.append(f"{cid}: code_entry must contain path and symbol")
        else:
            path = ROOT / entry["path"]
            if not path.exists():
                errors.append(f"{cid}: code_entry missing file {entry['path']}")
            elif entry["symbol"] not in _module_symbols(path):
                errors.append(f"{cid}: code_symbol {entry['symbol']} not found in {entry['path']}")
        errors.extend(_observation_errors(cid, item.get("observations")))
        for key in ("allowed_paths", "side_effects", "candidate_asset_families"):
            if not isinstance(item.get(key), list):
                errors.append(f"{cid}: {key} must be a list")
        remediation = item.get("remediation_kind")
        if item.get("status") != "supported":
            values = remediation if isinstance(remediation, list) else [remediation]
            if not values or any(value not in REMEDIATION for value in values):
                errors.append(f"{cid}: invalid remediation_kind {remediation!r}")
        nodeids = item.get("evidence_test_nodeids")
        if not isinstance(nodeids, list):
            errors.append(f"{cid}: evidence_test_nodeids must be a list")
            nodeids = []
        for nodeid in nodeids:
            if not _test_exists(nodeid):
                errors.append(f"{cid}: evidence test not found {nodeid}")
        if item.get("status") == "supported":
            if item.get("needs_new_instrumentation"):
                errors.append(f"{cid}: supported capability cannot need new instrumentation")
            if not nodeids:
                errors.append(f"{cid}: supported capability needs at least one evidence test")
            if item.get("oracle_type") == "none":
                errors.append(f"{cid}: supported capability needs an oracle")
            tokens = set()
            for observation in item.get("observations", []):
                tokens.add(str(observation.get("selector", "")).split(":")[-1])
                for field in observation.get("fields", []):
                    tokens.add(re.split(r"[.\[]", str(field))[0])
            if nodeids and not any(_test_mentions(nodeid, tokens) for nodeid in nodeids):
                errors.append(f"{cid}: evidence test does not mention any observation token")

    if probe_path is not None:
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
        records = probe.get("records") or []
        if probe.get("paid_requests") != 0 or probe.get("external_model_requests") != 0:
            errors.append("probe evidence must record zero paid/external requests")
        for item in capabilities:
            cid = item.get("capability_id")
            matching = [row for row in records if row.get("capability_id") == cid]
            for row in matching:
                if not _test_exists(row.get("test_nodeid", "")):
                    errors.append(f"{cid}: probe test not found {row.get('test_nodeid')}")
            if item.get("verification_status") == "verified":
                if not matching or any(row.get("result") != "pass" for row in matching):
                    errors.append(f"{cid}: verified capability has no passing probe record")

    unsupported = json.loads(unsupported_path.read_text(encoding="utf-8")).get("unsupported", [])
    for item in unsupported:
        cid = item.get("capability_id", "<missing>")
        if item.get("status") != "not_supported":
            errors.append(f"{cid}: unsupported entry must be not_supported")
        if not item.get("reason") or not item.get("evidence"):
            errors.append(f"{cid}: unsupported entry needs reason and evidence")
        if status_by_id.get(cid) not in (None, "not_supported"):
            errors.append(f"{cid}: listed in both matrix and unsupported")

    if catalog_path is not None:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        templates = catalog.get("templates") or []
        if catalog.get("group_count") != len(templates):
            errors.append("catalog group_count does not match templates")
        if catalog.get("record_count") != sum(item.get("record_count", 0) for item in templates):
            errors.append("catalog record_count does not match template sums")
        for item in templates:
            if item.get("recommended_split") not in {"dev", "release", "holdout"}:
                errors.append(f"{item.get('leakage_group_id')}: invalid split")
            if item.get("module") in (None, "unclassified", "conflict"):
                errors.append(f"{item.get('leakage_group_id')}: invalid module")
            family_flags = item.get("source_flags") or {}
            if family_flags:
                if item.get("prompt_dependent") != any(family_flags.values()):
                    errors.append(f"{item.get('leakage_group_id')}: prompt_dependent not conservative")
        generator = ROOT / "scripts/eval/agentbench/template_catalog.py"
        if catalog.get("generator_sha256") != hashlib.sha256(generator.read_bytes()).hexdigest():
            errors.append("catalog generator_sha256 does not match generator")
        if inventory_dir is not None:
            from scripts.eval.agentbench import template_catalog
            regenerated = template_catalog.build(inventory_dir)
            if regenerated.get("catalog_sha256") != catalog.get("catalog_sha256"):
                errors.append("catalog is not reproducible from the frozen inventory")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=pathlib.Path,
                        default=ROOT / "eval/agent/v1/capability-matrix.json")
    parser.add_argument("--unsupported", type=pathlib.Path,
                        default=ROOT / "eval/agent/v1/unsupported-capabilities.json")
    parser.add_argument("--catalog", type=pathlib.Path,
                        default=ROOT / "eval/agent/v1/template-catalog.json")
    parser.add_argument("--inventory", type=pathlib.Path, default=None)
    parser.add_argument("--probe", type=pathlib.Path,
                        default=ROOT / "eval/agent/v1/evidence-probe.json")
    args = parser.parse_args()
    errors = audit(args.matrix, args.unsupported, args.catalog, args.inventory, args.probe)
    matrix = json.loads(args.matrix.read_text(encoding="utf-8"))
    counts = {}
    for item in matrix["capabilities"]:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    print(json.dumps({"errors": errors, "passed": not errors, "counts": counts,
                      "total": len(matrix["capabilities"])}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
