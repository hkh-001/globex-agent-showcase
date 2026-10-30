from pathlib import Path

import pytest

import subprocess

from scripts.eval.run_public_business_baseline import (
    git_source_state,
    junit_execution,
    load_cases,
    source_is_stable,
    summarize,
)


def test_public_business_suite_is_unique_bounded_and_source_backed():
    suite = load_cases(Path("eval/business/public-cases-v1.json").resolve())
    assert len(suite["cases"]) == 18
    assert {case["domain"] for case in suite["cases"]} == {"retrieval", "transaction"}
    assert all(case["nodeid"].startswith("tests/") for case in suite["cases"])


def test_summary_does_not_mislabel_contracts_as_model_quality():
    rows = [
        {"domain": "retrieval", "metrics": ["constraint_satisfaction"], "passed": True},
        {"domain": "transaction", "metrics": ["authorization_safety"], "passed": False},
    ]
    summary = summarize(rows)
    assert summary["case_pass_rate"] == {"passed": 1, "total": 2, "rate": 0.5}
    assert summary["external_chat_model_access"] == "disabled"
    assert summary["physical_model_call_count"] == "not instrumented"
    assert "not model/agent answer quality" in summary["metric_scope"]


def test_suite_rejects_too_few_cases(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text('{"schema_version":"globex.public-business.v1","cases":[]}')
    with pytest.raises(ValueError, match="10-20"):
        load_cases(path)


@pytest.mark.parametrize("body,valid", [
    ('<testsuite><testcase classname="x" name="ok"/></testsuite>', True),
    ('<testsuite><testcase classname="x" name="skip"><skipped/></testcase></testsuite>', False),
    ('<testsuite tests="1"/>', False),
])
def test_junit_requires_one_actually_executed_pass(tmp_path, body, valid):
    path = tmp_path / "result.xml"
    path.write_text(body)
    assert junit_execution(path)["valid_single_pass"] is valid


def test_git_source_state_distinguishes_tracked_and_untracked_changes(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("v1\n")
    subprocess.run(["git", "add", "tracked.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    assert git_source_state(tmp_path) | {"source_commit": None} == {
        "source_commit": None, "tracked_worktree_clean": True,
        "working_tree_clean": True, "untracked_count": 0,
        "sampled_before_output_creation": True,
    }
    (tmp_path / "untracked.txt").write_text("local\n")
    state = git_source_state(tmp_path)
    assert state["tracked_worktree_clean"] is True
    assert state["working_tree_clean"] is False
    assert state["untracked_count"] == 1
    tracked.write_text("v2\n")
    assert git_source_state(tmp_path)["tracked_worktree_clean"] is False


def test_source_stability_requires_both_sampled_endpoints_clean():
    clean = {"source_commit": "a" * 40, "tracked_worktree_clean": True}
    dirty = {**clean, "tracked_worktree_clean": False}
    other = {"source_commit": "b" * 40, "tracked_worktree_clean": True}
    assert source_is_stable(clean, clean) is True
    assert source_is_stable(dirty, clean) is False
    assert source_is_stable(clean, dirty) is False
    assert source_is_stable(clean, other) is False
