from pathlib import Path

import pytest

import evalkit.schemas as schemas
from evalkit.config import ConfigError, load_run_config, load_task_config
from evalkit.schemas import (
    SCHEMA_VERSION,
    AgentScore,
    CalibrationLabel,
    Record,
    RunConfig,
    RunSummary,
    Threshold,
    Trajectory,
)

FIX = Path(__file__).resolve().parent.parent / "fixtures" / "results_v1"


def test_threshold_semantics():
    assert Threshold(metric="headline", op=">=", value=0.6).holds(0.6)
    assert not Threshold(metric="headline", op=">=", value=0.6).holds(0.59)
    assert Threshold(metric="cost_per_100_usd", op="<=", value=0.01).holds(0.009)
    with pytest.raises(ValueError):
        Threshold(metric="headline", op=">", value=0.6)  # type: ignore[arg-type]


def test_load_run_config_fixture():
    cfg = load_run_config(FIX / "run_config.yaml")
    assert cfg.models == ("sonnet", "haiku", "sol", "luna")
    assert cfg.max_cost_usd == 12 and len(cfg.thresholds) == 2


def test_load_task_config_fixture():
    task = load_task_config(FIX / "task_config.yaml")
    assert task.labels == ("yes", "no", "maybe")


def test_config_error_names_file_and_field(tmp_path):
    bad = tmp_path / "evalkit.yaml"
    bad.write_text(
        "models: [a]\njudges: [j]\nextractor: e\nthresholds:\n  - {metric: m, op: '>', value: 1}\n"
    )
    with pytest.raises(ConfigError) as exc:
        load_run_config(bad)
    msg = str(exc.value)
    assert str(bad) in msg and "thresholds.0.op" in msg


def test_missing_and_malformed_files(tmp_path):
    with pytest.raises(ConfigError, match="file not found"):
        load_run_config(tmp_path / "nope.yaml")
    (tmp_path / "list.yaml").write_text("- a\n- b\n")
    with pytest.raises(ConfigError, match="mapping"):
        load_run_config(tmp_path / "list.yaml")
    (tmp_path / "broken.yaml").write_text("models: [a\n")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_run_config(tmp_path / "broken.yaml")


def test_task_labels_must_be_unique_and_non_empty(tmp_path):
    base = (FIX / "task_config.yaml").read_text()
    (tmp_path / "dup.yaml").write_text(base.replace('["yes", "no", "maybe"]', '["yes", "yes"]'))
    with pytest.raises(ConfigError, match="labels"):
        load_task_config(tmp_path / "dup.yaml")
    (tmp_path / "empty.yaml").write_text(base.replace('["yes", "no", "maybe"]', "[]"))
    with pytest.raises(ConfigError, match="labels"):
        load_task_config(tmp_path / "empty.yaml")


def test_run_config_rejects_duplicates_and_bad_concurrency():
    with pytest.raises(ValueError, match="unique"):
        RunConfig(models=("a", "a"), judges=("j",), extractor="e")
    with pytest.raises(ValueError, match="concurrency"):
        RunConfig(models=("a",), judges=("j",), extractor="e", concurrency={"anthropic": 0})


@pytest.mark.contract
def test_schema_version_major_mismatch_is_rejected():
    assert SCHEMA_VERSION == "1.0"
    data = (
        (FIX / "record.json")
        .read_text()
        .replace('"schema_version": "1.0"', '"schema_version": "2.0"')
    )
    with pytest.raises(ValueError, match="not compatible"):
        Record.model_validate_json(data)
    minor = data.replace('"schema_version": "2.0"', '"schema_version": "1.3"')
    assert Record.model_validate_json(minor).schema_version == "1.3"


ROUND_TRIP = [
    (Record, "record.json"),
    (RunSummary, "run_summary.json"),
    (CalibrationLabel, "calibration_label.json"),
    (Trajectory, "trajectory.json"),
    (AgentScore, "agent_score.json"),
]


@pytest.mark.contract
@pytest.mark.parametrize(("model", "name"), ROUND_TRIP, ids=[n for _, n in ROUND_TRIP])
def test_fixtures_round_trip_losslessly_and_stably(model, name):
    first = model.model_validate_json((FIX / name).read_text())
    dumped = first.model_dump_json()
    second = model.model_validate_json(dumped)
    assert second == first
    assert second.model_dump_json() == dumped


@pytest.mark.contract
def test_run_config_round_trips():
    cfg = load_run_config(FIX / "run_config.yaml")
    assert RunConfig.model_validate_json(cfg.model_dump_json()) == cfg


@pytest.mark.contract
def test_every_public_name_is_importable():
    for name in schemas.__all__:
        assert getattr(schemas, name) is not None, name
