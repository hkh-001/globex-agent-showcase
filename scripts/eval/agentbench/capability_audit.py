"""Validate the AgentBench capability matrix, unsupported list and template catalog.

A capability may be marked supported only when it has a real code entry,
observable events and assertable fields.  Missing any of them must be recorded
as instrumentation_required or not_supported instead.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
REQUIRED = ("capability_id", "code_entry", "observable_events", "assertable_fields",
            "allowed_paths", "side_effects", "oracle_type", "status",
            "needs_new_instrumentation", "candidate_asset_families")
STATUSES = {"supported", "instrumentation_required", "not_supported"}
ORACLES = {"deterministic", "human", "none"}


def _code_entry_errors(code_entry: str) -> list[str]:
    if ":" not in code_entry:
        return [f"invalid code_entry {code_entry!r}"]
    path_text, line_text = code_entry.rsplit(":", 1)
    path = ROOT / path_text
    if not path.exists():
        return [f"code_entry missing file {path_text}"]
    try:
        line = int(line_text)
    except ValueError:
        return [f"invalid line in {code_entry!r}"]
    if line < 1 or line > len(path.read_text(encoding="utf-8").splitlines()):
        return [f"line out of range in {code_entry!r}"]
    return []


def audit(matrix_path: pathlib.Path, unsupported_path: pathlib.Path,
          catalog_path: pathlib.Path | None = None) -> list[str]:
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
        for key in REQUIRED:
            if key not in item:
                errors.append(f"{cid}: missing {key}")
        if item.get("status") not in STATUSES:
            errors.append(f"{cid}: invalid status")
        if item.get("oracle_type") not in ORACLES:
            errors.append(f"{cid}: invalid oracle_type")
        if not isinstance(item.get("needs_new_instrumentation"), bool):
            errors.append(f"{cid}: needs_new_instrumentation must be bool")
        errors.extend(f"{cid}: {message}" for message in _code_entry_errors(item.get("code_entry", "")))
        for key in ("observable_events", "assertable_fields", "allowed_paths", "side_effects",
                    "candidate_asset_families"):
            if not isinstance(item.get(key), list):
                errors.append(f"{cid}: {key} must be a list")
        if item.get("status") == "supported":
            if item.get("needs_new_instrumentation"):
                errors.append(f"{cid}: supported capability cannot need new instrumentation")
            if not item.get("observable_events") or not item.get("assertable_fields"):
                errors.append(f"{cid}: supported capability needs events and fields")
            if item.get("oracle_type") == "none":
                errors.append(f"{cid}: supported capability needs an oracle")
        if item.get("status") in {"instrumentation_required", "not_supported"} and not item.get(
                "needs_new_instrumentation"):
            errors.append(f"{cid}: {item.get('status')} must set needs_new_instrumentation")

    unsupported = json.loads(unsupported_path.read_text(encoding="utf-8")).get("unsupported", [])
    if not isinstance(unsupported, list) or not unsupported:
        errors.append("unsupported list is empty")
    for item in unsupported:
        cid = item.get("capability_id", "<missing>")
        if item.get("status") != "not_supported":
            errors.append(f"{cid}: unsupported entry must be not_supported")
        if not item.get("reason"):
            errors.append(f"{cid}: unsupported entry needs a reason")
        if not item.get("evidence"):
            errors.append(f"{cid}: unsupported entry needs evidence")
        if cid in seen and status_by_id.get(cid) != "not_supported":
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
            if item.get("module") == "unclassified":
                errors.append(f"{item.get('leakage_group_id')}: unclassified module")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--matrix", type=pathlib.Path,
                        default=ROOT / "eval/agent/v1/capability-matrix.json")
    parser.add_argument("--unsupported", type=pathlib.Path,
                        default=ROOT / "eval/agent/v1/unsupported-capabilities.json")
    parser.add_argument("--catalog", type=pathlib.Path,
                        default=ROOT / "eval/agent/v1/template-catalog.json")
    args = parser.parse_args()
    errors = audit(args.matrix, args.unsupported, args.catalog)
    print(json.dumps({"errors": errors, "passed": not errors}, ensure_ascii=False, indent=2))
    raise SystemExit(0 if not errors else 1)


if __name__ == "__main__":
    main()
