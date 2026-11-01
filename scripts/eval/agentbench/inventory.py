"""Schema-aware AgentBench Stage 1 asset inventory (v2.1).

Design notes
------------
* Every asset declares its id field, id normalization, version order, prompt
  field, split field and template namespace.  Versions collapse inside a family
  only; cross-family duplicates are reported, not silently merged.
* ``exact_prompt_sha256`` hashes the raw value (stable JSON for lists);
  ``normalized_prompt_sha256`` applies NFKC + whitespace collapse + strip and
  never strips version prefixes.  The two are reported separately.
* Leakage groups: ``shared-template:<namespace>:<template_id>`` when a trusted
  template id exists, otherwise ``prompt:<normalized_prompt_sha256>``.
* A per-record ``record-index.jsonl`` is written so splitting can be replayed;
  its hash is part of the manifest.
* Discovery walks ``eval/`` and classifies files into registered assets,
  unclassified candidates, ignored-with-reason and missing registered assets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import unicodedata
from datetime import datetime, timezone

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCAN_VERSION = "agentbench-inventory-v2.1"
PROMPT_NORMALIZATION = "nfkc+whitespace-v1"
CANDIDATE_KEYS = {"query", "question", "input", "checks", "steps",
                  "messages", "turns", "prompt", "instruction", "scenario"}

# path, kind, id_field, id_normalization, family, version, version_order,
# prompt_field, split_field, template_field, template_namespace,
# deterministic, prompt_dependent, allowed_usage
ASSETS = [
    ("eval/product_recall.jsonl", "retrieval_legacy", None, None, "retrieval-legacy", "v0", 0,
     "query", None, None, "retrieval-legacy", True, False, ["task_candidate", "oracle_source"]),
    ("eval/v1/product_retrieval.jsonl", "retrieval", "id", None, "retrieval-v1", "v1", 1,
     "query", None, None, "retrieval-v1", True, False, ["task_candidate", "oracle_source"]),
    ("eval/v2/product_retrieval.jsonl", "retrieval", "id", None, "retrieval-v2", "v1", 1,
     "query", "split", None, "retrieval-v2", True, False, ["task_candidate", "oracle_source"]),
    ("eval/v3/multilingual_retrieval.jsonl", "retrieval_multilingual", "id", None, "retrieval-v3", "v1", 1,
     "query", None, None, "retrieval-v3", True, False, ["task_candidate", "oracle_source"]),
    ("eval/v3/semantic_paraphrases.jsonl", "retrieval_paraphrase", "id", None, "retrieval-v3-paraphrase", "v1", 1,
     "query", None, "family", "retrieval-v3", True, False, ["oracle_source"]),
    ("eval/category_recall.jsonl", "retrieval_category", None, None, "retrieval-category", "v1", 1,
     "query", None, None, "retrieval-category", True, False, ["task_candidate", "oracle_source"]),
    ("eval/optimization_search.jsonl", "retrieval_optimization", "id", None, "retrieval-optimization", "v1", 1,
     "query", "split", None, "retrieval-optimization", True, False, ["task_candidate", "oracle_source"]),
    ("eval/retrieval/optimization-contract-queries-v1.jsonl", "retrieval_contract", "id", None,
     "optimization-contract", "v1", 1, "query", None, "family", "optimization-contract", True, False,
     ["oracle_source"]),
    ("eval/v1/knowledge_retrieval.jsonl", "knowledge", "id", None, "knowledge-v1", "v1", 1,
     "query", None, None, "knowledge-v1", True, False, ["task_candidate", "oracle_source"]),
    ("eval/business/public-cases-v1.json", "business_contract", "id", None, "business-public", "v1", 1,
     None, None, None, "business-public", True, False, ["oracle_source"]),
    ("eval/cases.yaml", "model_eval", "id", None, "model-eval", "v1", 1,
     "queries", None, None, "model-eval", False, True, ["oracle_source"]),
    ("eval/v1/agent_cases.yaml", "agent_case", "id", None, "agent-cases-v1", "v1", 1,
     "queries", "split", "template_family", "agent-cases-v1", False, True, ["task_candidate", "oracle_source"]),
    ("eval/harness/v1/cases.json", "harness_case", "id", None, "harness-cases", "v1-base", 1,
     "question", "split", "template_id", "harness-core", False, True, ["task_candidate", "oracle_source"]),
    ("eval/harness/v1/skill-catalog-cases.json", "harness_case", "id", None, "harness-cases", "v1-skill", 2,
     "question", "split", "template_id", "harness-core", False, True, ["task_candidate", "oracle_source"]),
    ("eval/harness/v1/cache-layout-cases.json", "harness_case", "id", None, "harness-cases", "v1-cache", 3,
     "question", "split", "template_id", "harness-core", False, True, ["task_candidate", "oracle_source"]),
    ("eval/context/cases.json", "context_case", "id", r"^v\d+-", "context-cases", "v1", 1,
     "question", "split", "template_id", "harness-core", False, True, ["task_candidate", "oracle_source"]),
    ("eval/context/cases-v2.json", "context_case", "id", r"^v\d+-", "context-cases", "v2", 2,
     "question", "split", "template_id", "harness-core", False, True, ["task_candidate", "oracle_source"]),
    ("eval/context/cases-v3.json", "context_case", "id", r"^v\d+-", "context-cases", "v3", 3,
     "question", "split", "template_id", "harness-core", False, True, ["task_candidate", "oracle_source"]),
    ("eval/learning/skill-cases-v2.json", "skill_case", "id", None, "skill-cases", "v2", 2,
     "query", "split", "template_id", "skill-cases", False, True, ["task_candidate", "oracle_source"]),
    ("eval/learning/v1.json", "learning_case", "id", None, "learning-v1", "v1", 1,
     "query", None, None, "learning-v1", False, True, ["oracle_source"]),
    ("eval/memory/cases.json", "memory_case", "id", None, "memory-cases", "v1", 1,
     "input", None, None, "memory-cases", False, True, ["task_candidate", "oracle_source"]),
    ("eval/product/dev/tasks.json", "task_candidate", "task_id", None, "product-dev", "v1", 1,
     None, "split", "template_family", "product-dev", True, False, ["task_candidate", "oracle_source"]),
    ("eval/product/dev-v2/tasks.json", "task_candidate", "task_id", None, "product-dev", "v2", 2,
     None, "split", "template_family", "product-dev", True, False, ["task_candidate", "oracle_source"]),
]

KEYS = ("path", "kind", "id_field", "id_normalization", "family", "version", "version_order",
        "prompt_field", "split_field", "template_field", "template_namespace",
        "deterministic", "prompt_dependent", "allowed_usage")
SPECS = [dict(zip(KEYS, row)) for row in ASSETS]
REGISTERED = {spec["path"] for spec in SPECS}

# Derived assets: (source family, reference field).  Their leakage group is
# bound to the referenced record's group instead of their own prompt hash.
DERIVED_ASSETS = {
    "eval/v3/semantic_paraphrases.jsonl": ("retrieval-v2", "reference_case_id"),
}

# kind -> (task_reusability, automatic_oracle_available)
REUSABILITY = {
    "retrieval_legacy": ("conversion_required", True),
    "retrieval": ("conversion_required", True),
    "retrieval_multilingual": ("conversion_required", True),
    "retrieval_paraphrase": ("oracle_only", True),
    "retrieval_category": ("conversion_required", True),
    "retrieval_optimization": ("conversion_required", True),
    "retrieval_contract": ("oracle_only", True),
    "knowledge": ("conversion_required", True),
    "business_contract": ("oracle_only", True),
    "model_eval": ("oracle_only", False),
    "agent_case": ("conversion_required", False),
    "harness_case": ("conversion_required", False),
    "context_case": ("conversion_required", False),
    "skill_case": ("conversion_required", False),
    "learning_case": ("oracle_only", False),
    "memory_case": ("conversion_required", False),
    "task_candidate": ("unresolved", True),
}

IGNORE_RULES = [
    (re.compile(r"eval/agent/v1-spike/.*\.json$"), "stage0_spec"),
    (re.compile(r"eval/harness/.*-suite\.json$|eval/harness/v1/suite\.json$"), "suite_config"),
    (re.compile(r"eval/.*/baseline[^/]*\.json$|eval/v1/baselines/.*\.json$"), "baseline"),
    (re.compile(r"eval/retrieval/optimization-(parameters|development|contract-manifest).*\.json$"), "experiment_config"),
    (re.compile(r"eval/retrieval/optimization-contract-catalog-.*\.jsonl$"), "catalog_fixture"),
    (re.compile(r"(^|/)manifest\.json$|eval/verification/.*\.json$"), "manifest_or_evidence"),
    (re.compile(r"eval/proposals/.*\.json$"), "proposal"),
    (re.compile(r"eval/ag-ui-verification-.*\.json$"), "evidence"),
    (re.compile(r"eval/product/.*/offline-contract-verification\.json$"), "evidence"),
    (re.compile(r"eval/context/baseline\.json$"), "baseline"),
]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def normalize_prompt(value):
    """Structure-preserving normalization: turns stay turns, not one string."""
    if value is None:
        return ""
    if isinstance(value, str):
        text = unicodedata.normalize("NFKC", value)
        return re.sub(r"\s+", " ", text).strip()
    if isinstance(value, (list, tuple)):
        return [normalize_prompt(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_prompt(item) for key, item in sorted(value.items())}
    return normalize_prompt(str(value))


def casefold_prompt(value):
    if isinstance(value, str):
        return value.casefold()
    if isinstance(value, list):
        return [casefold_prompt(item) for item in value]
    if isinstance(value, dict):
        return {key: casefold_prompt(item) for key, item in value.items()}
    return value


def prompt_hashes(value) -> tuple[str, str, str]:
    """Return (exact, normalized, casefold) hashes for a raw prompt value.

    ``exact`` hashes the raw value; lists/dicts use stable JSON, never a lossy
    space join.  ``normalized`` applies NFKC + whitespace collapse + strip.
    """
    if value is None or value == "":
        return "", "", ""
    raw = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)
    exact = digest(raw.encode())
    normalized_value = normalize_prompt(value)
    normalized_json = json.dumps(normalized_value, ensure_ascii=False, sort_keys=True)
    normalized = digest(normalized_json.encode()) if normalized_value != "" else ""
    casefold_json = json.dumps(casefold_prompt(normalized_value), ensure_ascii=False, sort_keys=True)
    casefold = digest(casefold_json.encode()) if normalized_value != "" else ""
    return exact, normalized, casefold


def load_rows(path: pathlib.Path) -> tuple[list[dict], bytes]:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    if path.suffix == ".jsonl":
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    elif path.suffix == ".yaml":
        parsed = yaml.safe_load(text)
        rows = parsed["cases"] if isinstance(parsed, dict) else parsed
    else:
        parsed = json.loads(text)
        rows = parsed if isinstance(parsed, list) else (
            parsed.get("cases") or parsed.get("tasks") or parsed.get("items") or [])
    if not isinstance(rows, list):
        raise SystemExit(f"{path}: unsupported asset shape")
    return rows, raw


def canonical_id(row: dict, spec: dict) -> str:
    id_field, normalization = spec["id_field"], spec["id_normalization"]
    if id_field is None:
        return digest(json.dumps(row, sort_keys=True, ensure_ascii=False).encode())
    value = str(row.get(id_field) or "")
    if normalization:
        value = re.sub(normalization, "", value)
    if not value:
        raise SystemExit(f"{spec['path']}: empty {id_field}")
    return value


def prompt_groups(rows: list[dict], spec: dict) -> list[dict]:
    """Deterministic duplicate/leakage grouping for one asset's records."""
    groups = []
    for row in rows:
        prompt = row.get(spec["prompt_field"]) if spec["prompt_field"] else None
        exact, normalized, casefold = prompt_hashes(prompt)
        template = row.get(spec["template_field"]) if spec["template_field"] else None
        record_hash = digest(json.dumps(row, sort_keys=True, ensure_ascii=False).encode())
        duplicate_group = f"exact-prompt:{exact}" if exact else f"record:{record_hash}"
        if template:
            leakage_group = f"shared-template:{spec['template_namespace']}:{str(template).strip()}"
        elif normalized:
            leakage_group = f"prompt:{normalized}"
        else:
            leakage_group = duplicate_group
        groups.append({"id": canonical_id(row, spec), "exact": exact, "normalized": normalized,
                       "casefold": casefold, "duplicate_group_id": duplicate_group,
                       "leakage_group_id": leakage_group})
    return groups


def select_latest(versions: list[tuple[str, int]]) -> str | None:
    """Pick the latest version by explicit order; duplicate orders are invalid."""
    seen = {}
    best = None
    for version, order in versions:
        if order in seen:
            raise SystemExit(f"duplicate version_order {order}: {seen[order]} and {version}")
        seen[order] = version
        if best is None or order > best[1]:
            best = (version, order)
    return best[0] if best else None


def fixture_resolution(tasks: list[dict]) -> tuple[str, bool]:
    hashes = {str(row.get("catalog_hash") or "") for row in tasks} - {""}
    if not hashes:
        return "not_applicable", True
    repo = {digest(path.read_bytes()) for pattern in ("data/*.jsonl", "eval/**/*.jsonl")
            for path in ROOT.glob(pattern)}
    resolved = hashes <= repo
    return ("resolved_by_raw_sha256" if resolved else "unresolved_by_raw_sha256", resolved)


def discover(root: pathlib.Path = ROOT) -> dict:
    found = sorted(path.relative_to(root).as_posix() for path in (root / "eval").rglob("*")
                   if path.is_file() and path.suffix in {".json", ".jsonl", ".yaml"})
    unclassified, ignored = [], []
    for rel in found:
        if rel in REGISTERED:
            continue
        reason = next((code for pattern, code in IGNORE_RULES if pattern.search(rel)), None)
        if reason:
            ignored.append({"path": rel, "reason": reason})
            continue
        try:
            rows, _ = load_rows(root / rel)
            looks_like_cases = bool(rows) and isinstance(rows[0], dict) and bool(
                CANDIDATE_KEYS & set(rows[0]))
        except Exception:
            looks_like_cases = False
        if looks_like_cases:
            unclassified.append({"path": rel, "reason": "no_schema_adapter"})
        else:
            ignored.append({"path": rel, "reason": "unsupported_schema"})
    missing = sorted(rel for rel in REGISTERED if not (root / rel).exists())
    return {"discovered": found, "unclassified_candidate_assets": unclassified,
            "ignored_with_reason": ignored, "missing_registered_assets": missing}


def scan(out: pathlib.Path) -> dict:
    assets, families = [], {}
    records, inputs = [], []
    for spec in SPECS:
        path = ROOT / spec["path"]
        if not path.exists():
            assets.append({"path": spec["path"], "status": "missing"})
            continue
        rows, raw = load_rows(path)
        inputs.append({"path": spec["path"], "sha256": digest(raw), "records": len(rows)})
        canonical, duplicates, splits = {}, [], {}
        groups = prompt_groups(rows, spec)
        for row, group in zip(rows, groups):
            if group["id"] in canonical:
                duplicates.append(group["id"])
            canonical[group["id"]] = row
            split_value = str(row.get(spec["split_field"])) if spec["split_field"] else ""
            if spec["split_field"]:
                splits[split_value] = splits.get(split_value, 0) + 1
            records.append({
                "asset_path": spec["path"], "record_id": group["id"],
                "family": spec["family"], "version": spec["version"],
                "split": split_value,
                "template_id": str(row.get(spec["template_field"])) if spec["template_field"] else "",
                "exact_prompt_sha256": group["exact"],
                "normalized_prompt_sha256": group["normalized"],
                "casefold_prompt_sha256": group["casefold"],
                "duplicate_group_id": group["duplicate_group_id"],
                "leakage_group_id": group["leakage_group_id"],
                "derived_from": "", "reference_case_id": "",
            })
        resolution, reusable = (fixture_resolution(rows) if spec["kind"] == "task_candidate"
                                else ("not_applicable", True))
        reusability, deterministic_oracle = REUSABILITY[spec["kind"]]
        if spec["kind"] == "task_candidate" and not reusable:
            reusability = "unresolved"
        assets.append({
            "path": spec["path"], "kind": spec["kind"], "family": spec["family"],
            "version": spec["version"], "version_order": spec["version_order"],
            "id_field": spec["id_field"], "id_normalization": spec["id_normalization"],
            "raw_record_count": len(rows), "unique_id_count": len(canonical),
            "duplicate_ids": sorted(set(duplicates)), "sha256": digest(raw),
            "deterministic": spec["deterministic"], "prompt_dependent": spec["prompt_dependent"],
            "allowed_usage": spec["allowed_usage"],
            "has_split_field": bool(spec["split_field"]), "split_distribution": splits,
            "fixture_resolution": resolution,
            "task_reusability": reusability,
            "automatic_oracle_defined": deterministic_oracle,
            "automatic_oracle_executable": deterministic_oracle and (
                spec["kind"] != "task_candidate" or reusable),
            "oracle_unavailable_reason": ("unresolved_catalog_fixture"
                                          if spec["kind"] == "task_candidate" and not reusable else ""),
            "prompt_count": sum(1 for group in groups if group["normalized"]),
            "leakage_group_count": len({group["leakage_group_id"] for group in groups}),
            "derived_from": None, "unresolved_references": 0,
            "status": "ok",
        })
        state = families.setdefault(spec["family"], {"versions": [], "effective_ids": set(), "raw_total": 0})
        state["versions"].append({"version": spec["version"], "order": spec["version_order"],
                                  "path": spec["path"], "raw": len(rows)})
        state["raw_total"] += len(rows)
        state["effective_ids"].update(canonical)

    reference_index = {row["record_id"]: row["leakage_group_id"]
                       for row in records if row["family"] == "retrieval-v2"}
    for path, (source_family, reference_field) in DERIVED_ASSETS.items():
        spec = next((item for item in SPECS if item["path"] == path), None)
        rows = load_rows(ROOT / path)[0] if path and (ROOT / path).exists() else []
        if spec is None or not rows:
            continue
        unresolved = 0
        for row, entry in zip(rows, (r for r in records if r["asset_path"] == path)):
            reference = str(row.get(reference_field) or "")
            entry["reference_case_id"] = reference
            entry["derived_from"] = source_family
            target = reference_index.get(reference)
            if target:
                entry["leakage_group_id"] = target
            else:
                unresolved += 1
        asset = next(item for item in assets if item["path"] == path)
        asset["derived_from"] = source_family
        asset["unresolved_references"] = unresolved
        asset["leakage_group_count"] = len({r["leakage_group_id"] for r in records if r["asset_path"] == path})

    exact_index, normalized_index = {}, {}
    for row in records:
        if row["exact_prompt_sha256"]:
            exact_index.setdefault(row["exact_prompt_sha256"], set()).add(row["family"])
        if row["normalized_prompt_sha256"]:
            normalized_index.setdefault(row["normalized_prompt_sha256"], set()).add(row["family"])
    cross_exact = sum(1 for families_set in exact_index.values() if len(families_set) > 1)
    cross_normalized = sum(1 for families_set in normalized_index.values() if len(families_set) > 1)

    family_rows = [{"family": name, "latest_version": select_latest(
                        [(item["version"], item["order"]) for item in state["versions"]]),
                    "versions": state["versions"], "raw_total": state["raw_total"],
                    "family_effective_cases": len(state["effective_ids"])}
                   for name, state in sorted(families.items())]
    discovery = discover()
    by_kind, raw_usage = {}, {}
    for entry in assets:
        if entry.get("status") != "ok":
            continue
        by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + entry["unique_id_count"]
        for usage in entry["allowed_usage"]:
            raw_usage[usage] = raw_usage.get(usage, 0) + entry["unique_id_count"]
    result = {
        "scan_version": SCAN_VERSION,
        "prompt_normalization": PROMPT_NORMALIZATION,
        "assets": assets,
        "families": family_rows,
        "by_kind": by_kind,
        "raw_records_by_allowed_usage": raw_usage,
        "cross_family_duplicated_prompt_values": cross_exact,
        "cross_family_duplicated_normalized_prompt_values": cross_normalized,
        "globally_unique_normalized_prompts": len(normalized_index),
        "discovery": discovery,
        "record_count": len(records),
        "totals": {
            "raw_records": sum(entry.get("raw_record_count", 0) for entry in assets),
            "family_effective_cases": sum(row["family_effective_cases"] for row in family_rows),
            "registered_assets": len(REGISTERED),
            "missing_registered_assets": len(discovery["missing_registered_assets"]),
            "unclassified_candidate_assets": len(discovery["unclassified_candidate_assets"]),
            "ignored_with_reason": len(discovery["ignored_with_reason"]),
        },
    }
    record_index_text = "\n".join(
        json.dumps(row, ensure_ascii=False, sort_keys=True) for row in records) + "\n"
    scanner_sha = digest(pathlib.Path(__file__).read_bytes())
    try:
        source_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
                                       capture_output=True, check=True).stdout.strip()
        tracked_clean = subprocess.run(["git", "diff", "--quiet", "HEAD", "--"],
                                       cwd=ROOT).returncode == 0
    except Exception:
        source_commit, tracked_clean = "unknown", False
    result.update(source_commit=source_commit, tracked_worktree_clean=tracked_clean,
                  scanner_sha256=scanner_sha,
                  record_index_sha256=digest(record_index_text.encode()))
    core = dict(result)
    result["scanned_at"] = datetime.now(timezone.utc).isoformat()
    result["deterministic_payload_sha256"] = digest(
        json.dumps(core, sort_keys=True, ensure_ascii=False).encode())
    out.mkdir(parents=True, exist_ok=False)
    (out / "source-inventory.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (out / "record-index.jsonl").write_text(record_index_text, encoding="utf-8")
    files = {path.name: digest(path.read_bytes()) for path in sorted(out.iterdir()) if path.is_file()}
    manifest = {"scan_version": SCAN_VERSION, "inputs": inputs, "outputs": files,
                "source_commit": result["source_commit"],
                "tracked_worktree_clean": result["tracked_worktree_clean"],
                "scanner_sha256": result["scanner_sha256"],
                "record_index_sha256": result["record_index_sha256"],
                "deterministic_payload_sha256": result["deterministic_payload_sha256"]}
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    result = scan(args.out.resolve())
    print(json.dumps({"totals": result["totals"], "record_count": result["record_count"],
                      "deterministic": result["deterministic_payload_sha256"][:16]}, ensure_ascii=False))
    for row in result["families"]:
        print(f"{row['family']:26s} raw={row['raw_total']:4d} effective={row['family_effective_cases']:4d} latest={row['latest_version']}")


if __name__ == "__main__":
    main()
