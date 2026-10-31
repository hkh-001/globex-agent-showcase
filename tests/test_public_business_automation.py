import re
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def _workflow(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text())


def _event_paths(workflow: dict, event: str) -> list[str]:
    # PyYAML 1.1 parses the unquoted GitHub Actions key `on` as boolean true.
    return workflow[True][event]["paths"]


def _github_glob_matches(pattern: str, path: str) -> bool:
    """Match the subset of GitHub path globs used by the two workflows."""
    expression = ""
    index = 0
    while index < len(pattern):
        if pattern[index : index + 3] == "**/":
            expression += "(?:.*/)?"
            index += 3
        elif pattern[index : index + 2] == "**":
            expression += ".*"
            index += 2
        elif pattern[index] == "*":
            expression += "[^/]*"
            index += 1
        elif pattern[index] == "?":
            expression += "[^/]"
            index += 1
        else:
            expression += re.escape(pattern[index])
            index += 1
    return re.fullmatch(expression, path) is not None


def _assert_paths_cover(workflow: dict, required_paths: list[str]) -> None:
    pull_request = _event_paths(workflow, "pull_request")
    push = _event_paths(workflow, "push")
    assert pull_request == push
    for path in required_paths:
        assert any(_github_glob_matches(pattern, path) for pattern in push), path


def test_make_baseline_is_credential_free_and_writes_to_ignored_runtime():
    makefile = (ROOT / "Makefile").read_text()
    assert ".PHONY: baseline" in makefile
    assert "baseline:" in makefile
    assert "scripts/eval/run_public_business_baseline.py --output $(PUBLIC_BASELINE_OUTPUT)" in makefile
    assert "PUBLIC_BASELINE_OUTPUT ?= .globex-runtime/" in makefile
    assert "LLM_API_KEY" not in makefile.split("baseline:", 1)[1].split("eval-harness:", 1)[0]


def test_offline_ci_runs_and_always_preserves_public_baseline():
    path = ROOT / ".github/workflows/harness-eval.yml"
    text = path.read_text()
    workflow = yaml.safe_load(text)
    safety = workflow["jobs"]["safety-contracts"]
    assert safety["env"]["LLM_API_KEY"] == ""
    steps = {step.get("name"): step for step in safety["steps"]}
    run = steps["Public business baseline without model credentials"]["run"]
    assert "scripts/eval/run_public_business_baseline.py" in run
    assert "runner.temp" in run and "--output" in run
    upload = steps["Preserve public business baseline"]
    assert upload["if"] == "always()"
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["with"]["path"] == "${{ runner.temp }}/globex-public-business"
    assert upload["with"]["if-no-files-found"] == "warn"
    assert upload["with"]["retention-days"] == 30
    assert "eval/business/**" in text


def test_paid_model_evaluation_requires_explicit_manual_opt_in():
    path = ROOT / ".github/workflows/harness-eval.yml"
    text = path.read_text()
    workflow = yaml.safe_load(text)
    dispatch = workflow[True]["workflow_dispatch"]
    opt_in = dispatch["inputs"]["run_model_evaluation"]
    model_job = workflow["jobs"]["model-evaluation"]
    assert model_job["if"] == "${{ github.event_name == 'workflow_dispatch' && inputs.run_model_evaluation }}"
    assert opt_in["type"] == "boolean"
    assert opt_in["default"] is False
    safety_job = workflow["jobs"]["safety-contracts"]
    assert "if" not in safety_job
    assert safety_job["env"]["LLM_API_KEY"] == ""


def test_backend_workflow_guards_backend_evaluation_and_public_evidence_changes():
    workflow = _workflow(".github/workflows/harness-eval.yml")
    _assert_paths_cover(
        workflow,
        [
            "app/domain/orders.py",
            "scripts/eval/run_public_business_baseline.py",
            "scripts/verify_public_evidence.py",
            "eval/harness/v1/suite.json",
            "eval/business/cases.json",
            "eval/verification/public/manifest.json",
            "tests/test_public_business_automation.py",
            ".github/workflows/harness-eval.yml",
            ".github/workflows/frontend-ci.yml",
        ],
    )
    assert not any(
        _github_glob_matches(pattern, "frontend/src/App.tsx")
        for pattern in _event_paths(workflow, "push")
    )


def test_frontend_workflow_guards_source_dependencies_and_audit_gate_changes():
    workflow = _workflow(".github/workflows/frontend-ci.yml")
    _assert_paths_cover(
        workflow,
        [
            "frontend/src/App.tsx",
            "frontend/src/components/styles.css",
            "frontend/tests/commerceClient.test.ts",
            "frontend/public/products/catalog.json",
            "frontend/package.json",
            "frontend/package-lock.json",
            "frontend/vite.config.ts",
            "frontend/tsconfig.json",
            "scripts/check_npm_audit.py",
            "security/audit-allowlist.json",
            "tests/test_npm_audit_gate.py",
            ".github/workflows/frontend-ci.yml",
        ],
    )
    assert not any(
        _github_glob_matches(pattern, "app/domain/orders.py")
        for pattern in _event_paths(workflow, "push")
    )


def test_frontend_workflow_runs_all_frontend_quality_gates():
    workflow = _workflow(".github/workflows/frontend-ci.yml")
    job = workflow["jobs"]["frontend"]
    steps = {step.get("name"): step for step in job["steps"]}
    assert "tests/test_npm_audit_gate.py" in steps["Audit gate contracts"]["run"]
    assert steps["Reject new or stale high-severity dependency exceptions"]["run"] == "python scripts/check_npm_audit.py"
    assert "test -- --run" in steps["Frontend tests"]["run"]
    assert "run build" in steps["Frontend production build"]["run"]
