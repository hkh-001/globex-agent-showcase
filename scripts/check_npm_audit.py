from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ALLOWLIST = ROOT / "security" / "audit-allowlist.json"
GHSA_PATTERN = re.compile(r"GHSA-[0-9a-z-]+", re.IGNORECASE)
BLOCKING_SEVERITIES = {"high", "critical"}
SEVERITY_RANK = {"info": 0, "low": 1, "moderate": 2, "high": 3, "critical": 4}


@dataclass
class AdvisoryFinding:
    severity: str = "high"
    packages: set[str] = field(default_factory=set)


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def _run_audit() -> dict[str, Any]:
    clean_env = {
        key: value
        for key, value in os.environ.items()
        if key.lower() not in {"all_proxy", "http_proxy", "https_proxy", "no_proxy"}
    }
    completed = subprocess.run(
        ["npm", "--prefix", "frontend", "audit", "--json", "--audit-level=high"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        env=clean_env,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        detail = completed.stderr.strip() or completed.stdout[-500:]
        raise RuntimeError(f"npm audit did not return valid JSON: {detail}") from exc
    if completed.returncode not in {0, 1}:
        raise RuntimeError(f"npm audit exited {completed.returncode}: {completed.stderr.strip()}")
    if payload.get("error"):
        raise RuntimeError(f"npm audit failed: {payload['error']}")
    return payload


def _advisory_id(via: dict[str, Any]) -> str:
    matches = {
        match.upper()
        for value in (via.get("url"), via.get("name"))
        if isinstance(value, str)
        for match in GHSA_PATTERN.findall(value)
    }
    if len(matches) != 1:
        raise ValueError(f"blocking advisory has no unique GHSA identity: {via!r}")
    return matches.pop()


def _blocking_findings(payload: dict[str, Any]) -> dict[str, AdvisoryFinding]:
    vulnerabilities = payload.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict):
        raise ValueError("npm audit JSON is missing the vulnerabilities object")
    findings: dict[str, AdvisoryFinding] = {}

    def resolve(package: str, stack: tuple[str, ...]) -> tuple[set[str], int]:
        if package in stack:
            raise ValueError(f"cyclic blocking vulnerability chain: {' -> '.join((*stack, package))}")
        raw = vulnerabilities.get(package)
        if not isinstance(raw, dict):
            raise ValueError(f"blocking vulnerability references missing package: {package}")
        severity = str(raw.get("severity", "")).lower()
        if severity not in SEVERITY_RANK:
            raise ValueError(f"vulnerability has invalid severity: {package}: {severity or '<missing>'}")
        via = raw.get("via")
        if not isinstance(via, list) or not via:
            raise ValueError(f"blocking vulnerability has no advisory path: {package}")
        identities: set[str] = set()
        via_max_rank = -1
        for item in via:
            if isinstance(item, str):
                child_identities, child_max_rank = resolve(item, (*stack, package))
                identities.update(child_identities)
                via_max_rank = max(via_max_rank, child_max_rank)
                continue
            if not isinstance(item, dict):
                raise ValueError(f"unsupported blocking advisory entry for {package}: {item!r}")
            advisory_severity = str(item.get("severity", "")).lower()
            if advisory_severity not in SEVERITY_RANK:
                raise ValueError(f"advisory has invalid severity: {item!r}")
            via_max_rank = max(via_max_rank, SEVERITY_RANK[advisory_severity])
            if advisory_severity not in BLOCKING_SEVERITIES:
                continue
            identity = _advisory_id(item)
            finding = findings.setdefault(identity, AdvisoryFinding())
            if SEVERITY_RANK[advisory_severity] > SEVERITY_RANK[finding.severity]:
                finding.severity = advisory_severity
            identities.add(identity)
        if via_max_rank < SEVERITY_RANK[severity]:
            raise ValueError(
                f"package severity is not explained by its advisory paths: {package}: {severity}"
            )
        if severity in BLOCKING_SEVERITIES and not identities:
            raise ValueError(f"blocking vulnerability did not resolve to an advisory: {package}")
        for identity in identities:
            findings[identity].packages.add(package)
        return identities, max(SEVERITY_RANK[severity], via_max_rank)

    for package, raw in vulnerabilities.items():
        if not isinstance(raw, dict):
            raise ValueError(f"invalid vulnerability entry: {package}")
        severity = str(raw.get("severity", "")).lower()
        if severity in BLOCKING_SEVERITIES:
            resolve(str(package), ())
    return findings


def check(payload: dict[str, Any], allowlist: dict[str, Any]) -> list[str]:
    try:
        actual = _blocking_findings(payload)
    except ValueError as exc:
        return [str(exc)]
    raw_allowed = allowlist.get("advisories")
    if not isinstance(raw_allowed, dict):
        return ["allowlist advisories must be an object"]
    errors: list[str] = []
    actual_ids = set(actual)
    allowed_ids = {str(item).upper() for item in raw_allowed}
    unexpected = sorted(actual_ids - allowed_ids)
    stale = sorted(allowed_ids - actual_ids)
    if unexpected:
        errors.append(f"unexpected blocking advisories: {', '.join(unexpected)}")
    if stale:
        errors.append(f"stale allowed advisories: {', '.join(stale)}")
    for identity in sorted(actual_ids & allowed_ids):
        entry = next((value for key, value in raw_allowed.items() if str(key).upper() == identity), None)
        if not isinstance(entry, dict):
            errors.append(f"invalid allowlist entry: {identity}")
            continue
        for field_name in ("reason", "remove_when"):
            if not str(entry.get(field_name, "")).strip():
                errors.append(f"missing {field_name} for {identity}")
        allowed_severity = str(entry.get("allowed_severity", "")).lower()
        if allowed_severity not in BLOCKING_SEVERITIES:
            errors.append(f"invalid allowed severity for {identity}: {allowed_severity or '<missing>'}")
        elif SEVERITY_RANK[actual[identity].severity] > SEVERITY_RANK[allowed_severity]:
            errors.append(f"severity escalation for {identity}: {allowed_severity} -> {actual[identity].severity}")
        allowed_packages = {str(item) for item in entry.get("affected_packages", [])}
        new_packages = sorted(actual[identity].packages - allowed_packages)
        stale_packages = sorted(allowed_packages - actual[identity].packages)
        if new_packages:
            errors.append(f"unexpected affected packages for {identity}: {', '.join(new_packages)}")
        if stale_packages:
            errors.append(f"stale affected packages for {identity}: {', '.join(stale_packages)}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail on new or stale high/critical npm audit findings.")
    parser.add_argument("--audit-json", type=Path, help="Use a saved npm audit JSON report.")
    parser.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    args = parser.parse_args(argv)
    try:
        payload = _load_json(args.audit_json) if args.audit_json else _run_audit()
        allowlist = _load_json(args.allowlist)
        errors = check(payload, allowlist)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"npm audit gate error: {exc}", file=sys.stderr)
        return 2
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    findings = _blocking_findings(payload)
    links = sum(len(value.packages) for value in findings.values())
    print(f"approved blocking audit findings: {len(findings)} advisory, {links} package links")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
