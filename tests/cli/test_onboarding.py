import json
import socket

import pytest

from evalkit import cli
from evalkit.onboarding import EXAMPLE_DIR, GITIGNORE_LINES

FAKE = "fake-onboarding-NEVER-PRINT-987654321"


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    return tmp_path


def no_network(monkeypatch):
    def refuse(*a, **k):
        raise OSError("network disabled in test")

    monkeypatch.setattr(socket, "create_connection", refuse)


# ---- ONB-01 init ------------------------------------------------------------------------------


def test_init_scaffolds_a_runnable_project_and_never_overwrites(project, capsys):
    assert cli.main(["init"]) == 0
    for f in ("evalkit.yaml", ".env.example", "tasks/my_task.yaml", "tasks/my_task.jsonl"):
        assert (project / f).is_file(), f
    gi = (project / ".gitignore").read_text(encoding="utf-8")
    assert all(line in gi.splitlines() for line in GITIGNORE_LINES)
    assert "ANTHROPIC_API_KEY=" in (project / ".env.example").read_text(encoding="utf-8")
    capsys.readouterr()
    code = cli.main(
        ["run", "evalkit.yaml", "--task", "tasks/my_task.yaml", "--n", "3", "--dry-run"]
    )
    assert code == 0 and "no API calls" in capsys.readouterr().out
    (project / "evalkit.yaml").write_text("models: [mine]\n", encoding="utf-8")
    assert cli.main(["init", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (
        "evalkit.yaml" in out["skipped"]
        and (project / "evalkit.yaml").read_text(encoding="utf-8") == "models: [mine]\n"
    )
    assert (project / ".gitignore").read_text(encoding="utf-8") == gi  # no duplicate lines


# ---- ONB-02 doctor ----------------------------------------------------------------------------


@pytest.mark.safety
def test_doctor_names_missing_keys_and_never_prints_values(project, capsys, monkeypatch):
    assert cli.main(["doctor", "--offline"]) == 2
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY not set" in out and "not ready" in out
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE)
    assert cli.main(["doctor", "--offline", "--json"]) == 0
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    assert payload["ready"] and FAKE not in raw
    names = {c["name"] for c in payload["checks"]}
    assert {"python", "sdk:anthropic", "key:anthropic", "prices", "cache", "network"} <= names


def test_doctor_reports_unreachable_hosts_without_failing(project, capsys, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE)
    no_network(monkeypatch)
    assert cli.main(["doctor", "--json"]) == 0  # network is a warning, keys are the blocker
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert (
        not checks["network:anthropic"]["ok"]
        and "unreachable" in checks["network:anthropic"]["detail"]
    )


def test_doctor_flags_stale_prices(monkeypatch):
    from datetime import date, timedelta

    from evalkit import onboarding

    age = onboarding.prices_age_days(date.today() + timedelta(days=200))
    assert age is not None and age >= 200
    monkeypatch.setattr(onboarding, "prices_age_days", lambda today=None: 120)
    prices = next(c for c in onboarding.doctor(offline=True) if c.name == "prices")
    assert not prices.ok and "older than 90" in prices.detail


# ---- ONB-03 report --example ------------------------------------------------------------------


def test_report_example_needs_no_keys_and_no_network(project, capsys, monkeypatch):
    no_network(monkeypatch)
    assert cli.main(["report", "--example", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    report = project / "evalkit-example" / "report.html"
    assert (
        out["example"]
        and report.is_file()
        and "Failure explorer" in report.read_text(encoding="utf-8")
    )
    assert out["caveats"]  # the example is uncalibrated and says so


def test_packaged_example_matches_the_golden_pipeline(tmp_path):
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location("bex", root / "scripts" / "build_example_run.py")
    assert spec and spec.loader
    bex = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bex)
    bex.build(tmp_path)
    for name in ("summary.json", "records.jsonl", "cases.jsonl", "run.json"):
        assert (tmp_path / name).read_bytes() == (EXAMPLE_DIR / name).read_bytes(), name


# ---- ONB-06 / ONB-08 json and caveats everywhere ----------------------------------------------


def test_errors_in_json_mode_print_one_object(project, capsys):
    assert cli.main(["report", "--json"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["exit_code"] == 2 and "--example" in out["error"]


def test_run_output_carries_caveats(project, capsys, monkeypatch):
    from evalkit import run as R
    from tests.golden.test_golden_run import reply
    from tests.pipeline.fakes import ScriptedBatchAdapter

    cli.main(["init"])
    capsys.readouterr()
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE)
    fake = ScriptedBatchAdapter(reply)
    monkeypatch.setattr(
        R, "make_adapters", lambda config, prices: {"anthropic": fake, "openai": fake}
    )
    code = cli.main(
        [
            "run",
            "evalkit.yaml",
            "--task",
            "tasks/my_task.yaml",
            "--n",
            "3",
            "--wait",
            "0.05",
            "--json",
        ]
    )
    assert code in (0, 1)
    out = json.loads(capsys.readouterr().out)
    assert any("not calibrated" in c for c in out["caveats"])
    code = cli.main(
        ["run", "evalkit.yaml", "--task", "tasks/my_task.yaml", "--n", "3", "--wait", "0.05"]
    )
    assert "⚠ judge not calibrated" in capsys.readouterr().out


# ---- B2.4 verifier regressions (L-30) ---------------------------------------------------------


def test_example_never_overwrites_a_real_run_folder(project, capsys):
    real = project / "runs" / "r-mine"
    real.mkdir(parents=True)
    (real / "run.json").write_text('{"run_id": "r-mine", "spent_usd": 0.057}', encoding="utf-8")
    assert cli.main(["report", "--example", "--out", str(real)]) == 2
    assert "not empty" in capsys.readouterr().err
    assert json.loads((real / "run.json").read_text(encoding="utf-8"))["spent_usd"] == 0.057
    assert cli.main(["report", "--example"]) == 0  # the default folder
    assert cli.main(["report", "--example"]) == 0  # and again: it holds the example


def test_init_force_never_touches_the_users_task_files(project, capsys):
    cli.main(["init"])
    capsys.readouterr()
    mine = '{"id": "my-1", "question": "q", "context": ["s"]}\n'
    (project / "tasks" / "my_task.jsonl").write_text(mine, encoding="utf-8")
    (project / "evalkit.yaml").write_text("models: [old]\n", encoding="utf-8")
    assert cli.main(["init", "--force", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (project / "tasks" / "my_task.jsonl").read_text(encoding="utf-8") == mine
    assert "tasks/my_task.jsonl" in out["skipped"] and "evalkit.yaml" in out["written"]


def test_init_writes_the_literal_ignores_and_handles_a_missing_newline(project):
    (project / ".gitignore").write_text("node_modules", encoding="utf-8")  # no trailing newline
    cli.main(["init"])
    lines = (project / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "node_modules" and {".env", "runs/", ".evalkit/"} <= set(lines)
    env_example = (project / ".env.example").read_text(encoding="utf-8")
    assert all(
        line.endswith("=") for line in env_example.splitlines() if line and not line.startswith("#")
    )  # names only, never values


def test_init_in_another_folder_says_cd_first(project, capsys):
    assert cli.main(["init", "sub/proj"]) == 0
    from pathlib import Path

    assert f"cd {Path('sub/proj')}" in capsys.readouterr().out  # platform separator


def test_doctor_blocks_on_missing_sdk_or_key_the_config_needs(project, capsys, monkeypatch):
    import importlib.util

    cli.main(["init"])
    capsys.readouterr()
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE)  # the scaffolded config also needs OpenAI
    assert cli.main(["doctor", "--offline", "--json"]) == 2
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert checks["key:openai"]["blocking"] and not checks["key:openai"]["ok"]
    monkeypatch.setenv("OPENAI_API_KEY", FAKE)
    real_find = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: None if name == "anthropic" else real_find(name, *a),
    )
    assert cli.main(["doctor", "--offline", "--json"]) == 2
    checks = {c["name"]: c for c in json.loads(capsys.readouterr().out)["checks"]}
    assert (
        checks["sdk:anthropic"]["blocking"]
        and "evalkit-harness[anthropic]" in checks["sdk:anthropic"]["detail"]
    )


def test_run_refuses_before_spending_when_an_sdk_is_missing(project, capsys, monkeypatch):
    import importlib.util

    cli.main(["init"])
    capsys.readouterr()
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE)
    real_find = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util,
        "find_spec",
        lambda name, *a: None if name == "openai" else real_find(name, *a),
    )
    assert cli.main(["run", "evalkit.yaml", "--task", "tasks/my_task.yaml", "--n", "3"]) == 2
    assert "pip install 'evalkit-harness[openai]'" in capsys.readouterr().err


def test_doctor_reads_dotenv_from_the_working_folder(project, capsys):
    (project / ".env").write_text(f"ANTHROPIC_API_KEY={FAKE}\n", encoding="utf-8")
    cli.main(["doctor", "--offline", "--json"])
    raw = capsys.readouterr().out
    checks = {c["name"]: c for c in json.loads(raw)["checks"]}
    assert checks["key:anthropic"]["ok"] and FAKE not in raw


def test_report_text_output_shows_caveats_and_example_notice(project, capsys):
    assert cli.main(["report", "--example"]) == 0
    out = capsys.readouterr().out
    assert "⚠ judge not calibrated" in out and "not real results" in out


def test_prices_age_uses_the_stalest_model(monkeypatch, tmp_path):
    from datetime import date

    from evalkit import cost, onboarding

    f = tmp_path / "prices.yaml"
    f.write_text("a: {checked_on: 2026-01-01}\nb: {checked_on: 2026-06-01}\n", encoding="utf-8")
    monkeypatch.setattr(cost, "PRICES_YAML", f)
    assert onboarding.prices_age_days(date(2026, 7, 1)) == 181
    f.write_text("a: {checked_on: not-a-date}\n", encoding="utf-8")
    assert onboarding.prices_age_days(date(2026, 7, 1)) is None  # warns, never crashes
