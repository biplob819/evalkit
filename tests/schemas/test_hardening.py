"""Regression tests for the N1.1 verifier findings (see docs/lessons.md L-8 to L-10)."""

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import evalkit.schemas as schemas
from evalkit.config import ConfigError, load_run_config, load_task_config
from evalkit.schemas import (
    AgentTask,
    Answer,
    CalibrationLabel,
    Completion,
    Interval,
    ModelSummary,
    Record,
    ResultSet,
    RunConfig,
    RunSummary,
    Step,
    Threshold,
)
from evalkit.schemas._base import FrozenDict

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "results_v1"


def record_data() -> dict[str, Any]:
    return json.loads((FIX / "record.json").read_text(encoding="utf-8"))


def summary_data() -> dict[str, Any]:
    return json.loads((FIX / "run_summary.json").read_text(encoding="utf-8"))


# ---- finding 1: no infinity or NaN anywhere --------------------------------------------


@pytest.mark.parametrize("bad", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_floats_are_rejected(bad):
    data = record_data()
    data["completion"]["cost_usd"] = bad
    with pytest.raises(ValidationError):
        Record.model_validate(data)
    with pytest.raises(ValidationError):
        Interval(point=bad, lo=bad, hi=bad)
    with pytest.raises(ValidationError):
        Threshold(metric="headline", op=">=", value=bad)


def test_yaml_infinity_cost_cap_is_rejected(tmp_path):
    base = (
        (FIX / "run_config.yaml")
        .read_text(encoding="utf-8")
        .replace("max_cost_usd: 12", "max_cost_usd: .inf")
    )
    (tmp_path / "c.yaml").write_text(base, encoding="utf-8")
    with pytest.raises(ConfigError, match="max_cost_usd"):
        load_run_config(tmp_path / "c.yaml")


# ---- finding 2: schema_version is required and well-formed ----------------------------


@pytest.mark.contract
@pytest.mark.parametrize("version", [None, "1", "1.x", "1.0.0.0", "", "01.0x"])
def test_schema_version_is_required_and_well_formed(version):
    for data, model in ((record_data(), Record), (summary_data(), RunSummary)):
        if version is None:
            data.pop("schema_version")
        else:
            data["schema_version"] = version
        with pytest.raises(ValidationError):
            model.model_validate(data)


@pytest.mark.contract
def test_run_summary_rejects_other_major_version():
    with pytest.raises(ValidationError, match="not compatible"):
        RunSummary.model_validate({**summary_data(), "schema_version": "2.0"})


# ---- finding 3: summary stamps cannot contradict each other ----------------------------


def _single(data: dict[str, Any]) -> dict[str, Any]:
    data = {**data, "judge_mode": "single", "self_preference": {}}
    data["models"] = [
        {
            **m,
            "headline": dict(list(m["headline"].items())[:1]),
            "groundedness_mean": dict(list(m["groundedness_mean"].items())[:1]),
        }
        for m in data["models"]
    ]
    data["judges"] = data["judges"][:1]
    return data


def _drop_judge(model: dict[str, Any], judge: str) -> None:
    model["headline"].pop(judge)
    model["groundedness_mean"].pop(judge)


def test_valid_single_judge_calibrated_summary():
    assert RunSummary.model_validate(_single(summary_data())).judge_mode == "single"


@pytest.mark.parametrize(
    ("label", "mutate", "message"),
    [
        (
            "duplicate judge",
            lambda d: d.update(judges=[d["judges"][0], d["judges"][0]]),
            "only once",
        ),
        (
            "uncalibrated with agreement",
            lambda d: d.update(calibration_status="uncalibrated"),
            "uncalibrated run cannot",
        ),
        ("complete without finished_at", lambda d: d.update(finished_at=None), "finished_at"),
        (
            "finished before started",
            lambda d: d.update(finished_at="2026-11-14T15:00:00Z"),
            "before started_at",
        ),
        (
            "pending with finished_at",
            lambda d: d.update(judging_status="pending"),
            "finished_at",
        ),
        (
            "headline count mismatch",
            lambda d: _drop_judge(d["models"][0], "gpt-6.1-sol"),
            "headline entries",
        ),
    ],
)
def test_inconsistent_run_summary_is_rejected(label, mutate, message):
    data = summary_data()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        RunSummary.model_validate(data)


def test_single_judge_run_cannot_report_self_preference():
    data = _single(summary_data())
    data["self_preference"] = summary_data()["self_preference"]
    with pytest.raises(ValidationError, match="self-preference needs two judges"):
        RunSummary.model_validate(data)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m: m.update(n_errors={"timeout": 1}), "unknown error types"),
        (lambda m: m.update(n_errors={"schema": 500}), "cannot exceed n"),
        (lambda m: m.update(latency_p95_ms=10), "p95"),
        (lambda m: m["headline"].pop("gpt-6.1-sol"), "same judges"),
    ],
)
def test_inconsistent_model_summary_is_rejected(mutate, message):
    m = summary_data()["models"][0]
    mutate(m)
    with pytest.raises(ValidationError, match=message):
        ModelSummary.model_validate(m)


# ---- finding 4: deep immutability --------------------------------------------------------


def test_records_are_deeply_immutable_and_hashable():
    rec = Record.model_validate(record_data())
    with pytest.raises(AttributeError):
        rec.claims.append(rec.claims[0])  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        rec.completion.params["temperature"] = 99  # type: ignore[index]
    assert isinstance(rec.completion.params, FrozenDict)
    assert hash(rec) == hash(Record.model_validate(record_data()))


def test_free_form_json_is_frozen_recursively():
    step = Step(
        index=0,
        tool="run_sql",
        arguments={"q": {"filters": [1, 2]}},
        result_summary="",
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
    )
    with pytest.raises(TypeError):
        step.arguments["q"]["filters"] = []  # type: ignore[index]
    assert step.arguments["q"]["filters"] == (1, 2)
    again = Step.model_validate_json(step.model_dump_json())
    assert again == step


def test_sets_are_rejected_in_free_form_json():
    with pytest.raises(ValidationError, match="sets"):
        Step(
            index=0,
            tool="t",
            arguments={"x": {1, 2}},
            result_summary="",
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
        )


# ---- finding 5: strict numbers and linked records ---------------------------------------


@pytest.mark.parametrize("bad", [True, "5", 5.0])
def test_token_counts_are_strict_ints(bad):
    data = record_data()
    data["completion"]["tokens_in"] = bad
    with pytest.raises(ValidationError):
        Record.model_validate(data)


@pytest.mark.parametrize("bad", [[True], ["2"], [1.0]])
def test_citations_are_strict_ints(bad):
    with pytest.raises(ValidationError):
        Answer(label="yes", answer="a", citations=bad)


def test_success_without_answer_is_rejected():
    with pytest.raises(ValidationError, match="must carry an answer"):
        Completion(
            model="m",
            model_snapshot="",
            params={},
            raw_text="",
            answer=None,
            tokens_in=0,
            tokens_out=0,
            latency_ms=0,
            cost_usd=0,
        )


def test_record_links_claims_and_verdicts():
    data = record_data()
    data["claims"].append(data["claims"][0])
    with pytest.raises(ValidationError, match="unique"):
        Record.model_validate(data)
    data = record_data()
    data["verdicts"][0]["claim_id"] = "ghost"
    with pytest.raises(ValidationError, match="not in this record"):
        Record.model_validate(data)
    data = record_data()
    data["verdicts"][1]["judge"] = data["verdicts"][0]["judge"]
    with pytest.raises(ValidationError, match="at most one verdict"):
        Record.model_validate(data)


def test_naive_timestamps_and_blank_labels_are_rejected():
    data = record_data()
    data["timestamp"] = "2026-11-14T16:00:00"
    with pytest.raises(ValidationError):
        Record.model_validate(data)
    with pytest.raises(ValidationError):
        Answer(label=" ", answer="a", citations=())


# ---- finding 6: loader errors name the file ----------------------------------------------


def test_non_utf8_config_names_the_file(tmp_path):
    f = tmp_path / "latin1.yaml"
    f.write_bytes(b"models: [caf\xe9]\n")
    with pytest.raises(ConfigError, match="not UTF-8") as exc:
        load_run_config(f)
    assert str(f) in str(exc.value)


def test_directory_and_duplicate_keys(tmp_path):
    with pytest.raises(ConfigError, match="is a directory"):
        load_run_config(tmp_path)
    f = tmp_path / "dup.yaml"
    f.write_text("models: [a]\nmodels: [b]\njudges: [j]\nextractor: e\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate key 'models'"):
        load_run_config(f)


@pytest.mark.parametrize(
    "fragment",
    ["models: ['']", "judges: ['  ']", "models: [a, A]", "max_output_tokens: '2048'"],
)
def test_run_config_rejects_blank_duplicate_or_coerced_values(tmp_path, fragment):
    key = fragment.split(":")[0]
    base = {
        "models": "models: [a]",
        "judges": "judges: [j]",
        "extractor": "extractor: e",
        "max_output_tokens": "max_output_tokens: 10",
    }
    base[key] = fragment
    f = tmp_path / "c.yaml"
    f.write_text("\n".join(base.values()) + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=key):
        load_run_config(f)


@pytest.mark.parametrize("labels", ['["yes", "Yes"]', '[" yes", "no"]', '["yes", ""]'])
def test_task_labels_reject_case_duplicates_and_edge_spaces(tmp_path, labels):
    base = (
        (FIX / "task_config.yaml")
        .read_text(encoding="utf-8")
        .replace('["yes", "no", "maybe"]', labels)
    )
    (tmp_path / "t.yaml").write_text(base, encoding="utf-8")
    with pytest.raises(ConfigError, match="labels"):
        load_task_config(tmp_path / "t.yaml")


def test_scorers_cannot_be_blank(tmp_path):
    base = (FIX / "task_config.yaml").read_text(encoding="utf-8")
    base = base.replace(
        "scorers: [label_correct, citation_valid, groundedness, coverage, cost, latency]",
        "scorers: ['']",
    )
    (tmp_path / "t.yaml").write_text(base, encoding="utf-8")
    with pytest.raises(ConfigError, match="scorers"):
        load_task_config(tmp_path / "t.yaml")


# ---- finding 7: agent schema edges ------------------------------------------------------


def test_result_set_rows_must_match_columns():
    with pytest.raises(ValidationError, match="do not have 2 values"):
        ResultSet(columns=("a", "b"), rows=((1, 2), (3,)))


def test_simulated_user_intent_is_required_even_if_null():
    with pytest.raises(ValidationError, match="simulated_user_intent"):
        AgentTask.model_validate(
            {
                "id": "t",
                "source_project": "p",
                "goal": "g",
                "allowed_tools": ["x"],
                "step_budget": 3,
                "gold_outcome": {"kind": "attack_blocked", "attack": "a"},
                "split": "public",
            }
        )


# ---- finding 8: proofs that can fail -----------------------------------------------------

TECH_SPEC_NAMES = {
    "Case",
    "Answer",
    "Completion",
    "Claim",
    "ClaimVerdict",
    "Scores",
    "Record",
    "Interval",
    "ModelSummary",
    "JudgeAgreement",
    "RunSummary",
    "CalibrationLabel",
    "Threshold",
    "RunConfig",
    "TaskConfig",
    "AgentTask",
    "GoldOutcome",
    "ResultSet",
    "AttackBlocked",
    "Step",
    "Trajectory",
    "AgentScore",
    "SCHEMA_VERSION",
}


@pytest.mark.contract
def test_every_tech_spec_schema_is_exported():
    missing = TECH_SPEC_NAMES - set(schemas.__all__)
    assert not missing, f"not exported from evalkit.schemas: {sorted(missing)}"
    for name in TECH_SPEC_NAMES:
        assert hasattr(schemas, name), name


@pytest.mark.contract
def test_run_config_json_fixture_round_trips_stably():
    first = RunConfig.model_validate_json((FIX / "run_config.json").read_text(encoding="utf-8"))
    dumped = first.model_dump_json()
    assert RunConfig.model_validate_json(dumped) == first
    assert RunConfig.model_validate_json(dumped).model_dump_json() == dumped


@pytest.mark.contract
def test_calibration_label_contract():
    lab = CalibrationLabel.model_validate_json(
        (FIX / "calibration_label.json").read_text(encoding="utf-8")
    )
    assert CalibrationLabel.model_validate_json(lab.model_dump_json()) == lab
    assert set(CalibrationLabel.model_fields) == {"claim_id", "split", "label", "extraction_error"}
    with pytest.raises(ValidationError):
        CalibrationLabel.model_validate({**lab.model_dump(), "label": "maybe"})
