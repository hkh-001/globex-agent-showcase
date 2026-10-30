from __future__ import annotations

from copy import deepcopy

import pytest

from scripts.check_npm_audit import _run_audit, check

ALLOWLIST = {
    "advisories": {
        "GHSA-mrhx-c7xc-m3w5": {
            "allowed_severity": "high",
            "affected_packages": ["@ag-ui/client", "zod"],
            "reason": "No compatible upstream fix.",
            "remove_when": "A compatible fixed release is available.",
        }
    }
}
REPORT = {
    "vulnerabilities": {
        "@ag-ui/client": {"severity": "high", "via": ["zod"]},
        "zod": {
            "severity": "high",
            "via": [{"severity": "high", "url": "https://github.com/advisories/GHSA-mrhx-c7xc-m3w5"}],
        },
        "dev-only-warning": {"severity": "moderate", "via": []},
    }
}


def test_exact_reviewed_findings_pass_and_moderate_is_ignored() -> None:
    assert check(REPORT, ALLOWLIST) == []


def test_new_blocking_advisory_in_existing_package_fails() -> None:
    report = deepcopy(REPORT)
    report["vulnerabilities"]["zod"]["via"].append(
        {"severity": "high", "url": "https://github.com/advisories/GHSA-1111-2222-3333"}
    )
    assert any("unexpected blocking advisories" in item for item in check(report, ALLOWLIST))


def test_new_moderate_advisory_in_existing_high_package_is_not_blocking() -> None:
    report = deepcopy(REPORT)
    report["vulnerabilities"]["zod"]["via"].append(
        {"severity": "moderate", "url": "https://github.com/advisories/GHSA-1111-2222-3333"}
    )
    assert check(report, ALLOWLIST) == []


def test_unidentified_critical_advisory_fails() -> None:
    report = deepcopy(REPORT)
    report["vulnerabilities"]["zod"]["via"].append(
        {"severity": "critical", "url": "https://example.invalid/advisory/42"}
    )
    assert any("no unique GHSA identity" in item for item in check(report, ALLOWLIST))


def test_severity_escalation_fails() -> None:
    report = deepcopy(REPORT)
    report["vulnerabilities"]["zod"]["severity"] = "critical"
    assert any("package severity is not explained" in item for item in check(report, ALLOWLIST))


def test_missing_string_dependency_fails() -> None:
    report = deepcopy(REPORT)
    report["vulnerabilities"]["@ag-ui/client"]["via"].append("missing-package")
    assert any("references missing package" in item for item in check(report, ALLOWLIST))


def test_cyclic_string_dependencies_fail() -> None:
    report = {"vulnerabilities": {"a": {"severity": "high", "via": ["b"]}, "b": {"severity": "high", "via": ["a"]}}}
    assert any("cyclic blocking vulnerability chain" in item for item in check(report, {"advisories": {}}))


def test_stale_exception_fails_after_upstream_fix() -> None:
    assert any("stale allowed advisories" in item for item in check({"vulnerabilities": {}}, ALLOWLIST))


def test_new_and_stale_affected_package_links_fail() -> None:
    report = deepcopy(REPORT)
    report["vulnerabilities"]["new-wrapper"] = {"severity": "high", "via": ["zod"]}
    assert any("unexpected affected packages" in item for item in check(report, ALLOWLIST))
    allowlist = deepcopy(ALLOWLIST)
    allowlist["advisories"]["GHSA-mrhx-c7xc-m3w5"]["affected_packages"].append("fixed-wrapper")
    assert any("stale affected packages" in item for item in check(REPORT, allowlist))


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr"),
    [(2, "{}", "network unavailable"), (1, "not json", "gateway error"), (1, '{"error":{"code":"EAUDITENDPOINT"}}', "")],
)
def test_audit_transport_and_format_errors_fail_closed(monkeypatch, returncode, stdout, stderr) -> None:
    completed = type("Completed", (), {"returncode": returncode, "stdout": stdout, "stderr": stderr})()
    monkeypatch.setattr("subprocess.run", lambda *args, **kwargs: completed)
    with pytest.raises(RuntimeError):
        _run_audit()


def test_audit_scrubs_proxy_variables(monkeypatch) -> None:
    captured = {}
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:2")

    def fake_run(*args, **kwargs):
        captured.update(kwargs["env"])
        return type("Completed", (), {"returncode": 0, "stdout": '{"vulnerabilities":{}}', "stderr": ""})()

    monkeypatch.setattr("subprocess.run", fake_run)
    assert _run_audit() == {"vulnerabilities": {}}
    assert "ALL_PROXY" not in captured
    assert "HTTPS_PROXY" not in captured
