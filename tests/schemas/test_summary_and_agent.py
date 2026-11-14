import json
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from evalkit.schemas import (
    AgentTask,
    AttackBlocked,
    CalibrationLabel,
    GoldOutcome,
    Interval,
    ModelSummary,
    ResultSet,
    RunSummary,
)

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "results_v1"


def summary_data():
    return json.loads((FIX / "run_summary.json").read_text(encoding="utf-8"))


def test_interval_must_be_ordered():
    Interval(point=0.5, lo=0.4, hi=0.6)
    for bad in ({"point": 0.3, "lo": 0.4, "hi": 0.6}, {"point": 0.7, "lo": 0.4, "hi": 0.6}):
        with pytest.raises(ValidationError, match="lo <= point <= hi"):
            Interval(**bad)


def test_model_summary_threshold_flags_must_agree():
    m = summary_data()["models"][0]
    ModelSummary.model_validate(m)
    with pytest.raises(ValidationError, match="passes_threshold"):
        ModelSummary.model_validate({**m, "failed_thresholds": ["headline"]})


def test_run_summary_carries_honest_stamps():
    s = RunSummary.model_validate(summary_data())
    assert s.judge_mode == "dual" and s.calibration_status == "calibrated"
    assert len(s.judges) == 2


def test_calibrated_dual_run_needs_both_judges():
    data = summary_data()
    data["judges"] = data["judges"][:1]
    with pytest.raises(ValidationError, match="needs 2 judge"):
        RunSummary.model_validate(data)


def test_uncalibrated_single_judge_run_is_valid_without_agreement():
    data = {**summary_data(), "judge_mode": "single", "calibration_status": "uncalibrated"}
    data["judges"] = []
    data["self_preference"] = {}
    data["models"] = [
        {
            **m,
            "headline": dict(list(m["headline"].items())[:1]),
            "groundedness_mean": dict(list(m["groundedness_mean"].items())[:1]),
        }
        for m in data["models"]
    ]
    assert RunSummary.model_validate(data).judge_mode == "single"


def test_bad_stamp_values_are_rejected():
    for field, bad in (("judge_mode", "triple"), ("calibration_status", "sort-of")):
        with pytest.raises(ValidationError):
            RunSummary.model_validate({**summary_data(), field: bad})


def test_calibration_label_values():
    lab = CalibrationLabel.model_validate_json(
        (FIX / "calibration_label.json").read_text(encoding="utf-8")
    )
    assert lab.split == "heldout" and not lab.extraction_error
    with pytest.raises(ValidationError):
        CalibrationLabel(claim_id="c", split="train", label="supported")  # type: ignore[arg-type]


def test_gold_outcome_is_a_discriminated_union():
    ta = TypeAdapter(GoldOutcome)
    assert isinstance(
        ta.validate_python({"kind": "attack_blocked", "attack": "inj"}), AttackBlocked
    )
    rs = ta.validate_python({"kind": "result_set", "columns": ["n"], "rows": [[1]]})
    assert isinstance(rs, ResultSet) and rs.ordered is False
    with pytest.raises(ValidationError):
        ta.validate_python({"kind": "vibes"})


def test_agent_task_split_and_free_source_project():
    t = AgentTask(
        id="agt-1",
        source_project="anything-a-user-names",
        goal="g",
        allowed_tools=("run_sql",),
        step_budget=6,
        gold_outcome=AttackBlocked(attack="inj"),
        simulated_user_intent=None,
        split="heldout",
    )
    assert t.split == "heldout"
    with pytest.raises(ValidationError):
        AgentTask.model_validate({**t.model_dump(), "split": "private"})
