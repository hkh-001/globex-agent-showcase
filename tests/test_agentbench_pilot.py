"""Contract tests for the deterministic Stage 2 pilot candidates."""
from __future__ import annotations

import json
import pathlib

from scripts.eval.agentbench import generate_pilot, validate_pilot

ROOT = pathlib.Path(__file__).resolve().parents[1]
CATALOG = ROOT / "eval/agent/v1/template-catalog.json"
MATRIX = ROOT / "eval/agent/v1/capability-matrix.json"
PILOT = ROOT / "eval/agent/v2/pilot-candidates.json"


def test_generation_is_deterministic():
    assert generate_pilot.build() == generate_pilot.build()


def test_committed_pilot_matches_generator():
    assert generate_pilot.build() == json.loads(PILOT.read_text(encoding="utf-8"))


def test_pilot_is_small_and_dev_only():
    data = json.loads(PILOT.read_text(encoding="utf-8"))
    assert 1 <= data["candidate_count"] <= 6 < 60
    assert data["formal_eval_size"] is False
    assert data["holdout_used"] is False
    tasks = data["tasks"]
    assert all(task["split"] == "dev" for task in tasks)
    assert len({task["leakage_group_id"] for task in tasks}) == len(tasks)


def test_every_group_is_a_dev_catalog_group():
    data = json.loads(PILOT.read_text(encoding="utf-8"))
    catalog = {item["leakage_group_id"]: item for item in
               json.loads(CATALOG.read_text(encoding="utf-8"))["templates"]}
    for task in data["tasks"]:
        group = catalog[task["leakage_group_id"]]
        assert group["recommended_split"] == "dev"
        assert group["module"] == task["module"]


def test_capabilities_are_verified_and_isolated():
    data = json.loads(PILOT.read_text(encoding="utf-8"))
    verified = {item["capability_id"] for item in
                json.loads(MATRIX.read_text(encoding="utf-8"))["capabilities"]
                if item.get("verification_status") == "verified"}
    for task in data["tasks"]:
        assert task["required_capabilities"]
        assert set(task["required_capabilities"]) <= verified
        assert task["deterministic_checks"]
        assert task["isolation"]["database"] == "fresh_per_task"
        assert task["turns"] and task["source_record_id"] and task["source_prompt_sha256"]
        assert task["automatic_scoring"] is False
        assert task["execution_ready"] is False
        assert validate_pilot.validate(PILOT) == []
        assert task["side_effect_policy"]["before_after_snapshot"] in {"none", "trade_repository"}
        assert task["max_turns"] >= 1 and task["max_model_calls"] >= 0
