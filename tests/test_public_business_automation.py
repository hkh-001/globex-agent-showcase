from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


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
