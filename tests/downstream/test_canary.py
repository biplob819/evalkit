"""DSC-03: the canary runs each downstream project's EvalKit-importing tests against this
checkout, offline and without keys, and records the result (D-61)."""

from __future__ import annotations

import json
import re
import sys
import textwrap
from pathlib import Path

import yaml

import downstream_canary as dc
from harness_lib import ROOT

WORKFLOW = ROOT / ".github" / "workflows" / "downstream.yml"
BASELINE = sorted((ROOT / "docs" / "measurements").glob("downstream-canary-*.json"))


def _workflow() -> dict:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    data["on"] = data.pop(True, data.get("on"))  # YAML 1.1 reads a bare `on` as true
    return data


def test_workflow_triggers_and_least_privilege():
    wf = _workflow()
    assert set(wf["on"]) == {"schedule", "workflow_dispatch", "push"}
    assert wf["on"]["push"] == {"tags": ["v*"]}
    assert wf["permissions"] == {"contents": "read"}
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text
    assert not re.search(r"API_KEY|_TOKEN|PASSWORD", text)


def test_workflow_matrix_comes_from_the_registry_and_does_not_fail_fast():
    wf = _workflow()
    plan, canary = wf["jobs"]["plan"], wf["jobs"]["canary"]
    plan_run = " ".join(s.get("run", "") for s in plan["steps"])
    assert "downstream_canary.py --matrix" in plan_run
    assert canary["needs"] == "plan"
    assert canary["strategy"]["fail-fast"] is False
    assert "fromJSON(needs.plan.outputs.matrix)" in canary["strategy"]["matrix"]
    run = " ".join(s.get("run", "") for s in canary["steps"])
    assert "downstream_canary.py --run-one" in run
    uses = [s.get("uses", "") for s in canary["steps"]]
    assert any(u.startswith("actions/upload-artifact@") for u in uses)
    checkouts = [s for s in canary["steps"] if s.get("uses", "").startswith("actions/checkout@")]
    assert any(s.get("with", {}).get("repository") == "${{ matrix.repo }}" for s in checkouts)
    assert all(s["with"].get("persist-credentials") is False for s in checkouts), "no token on disk"


def test_matrix_holds_only_projects_with_tests(capsys):
    assert dc.main(["--matrix"]) == 0
    matrix = json.loads(capsys.readouterr().out)
    auto = [r for r in dc.load_registry() if r.tests in {"auto", "all"}]
    assert [m["project"] for m in matrix["include"]] == [r.project for r in auto]
    assert all(set(m) == {"project", "repo", "branch"} for m in matrix["include"])


def test_commands_install_this_checkout_over_the_pin_and_skip_live_tests(tmp_path):
    (row,) = [r for r in dc.load_registry() if r.project == "ai-visibility"]
    cmds = dc.commands(row, tmp_path / "evalkit", ["tests/test_a.py", "tests/test_b.py"])
    assert cmds[0] == ["uv", "sync", "--frozen", "--extra", "support"]
    assert cmds[1] == ["uv", "pip", "install", "--no-deps", "-e", str(tmp_path / "evalkit")]
    assert cmds[2][:3] == ["uv", "run", "--no-sync"]
    assert cmds[2][cmds[2].index("-m") + 1] == "not live"
    assert cmds[2][-2:] == ["tests/test_a.py", "tests/test_b.py"]
    assert not any(c.startswith("--deselect") for c in cmds[2])


def test_pin_identity_tests_are_deselected_by_node_id(tmp_path):
    (row,) = [r for r in dc.load_registry() if r.project == "gemma-grounded-health"]
    cmd = dc.commands(row, tmp_path, ["tests/test_a.py"])[2]
    deselected = [c.removeprefix("--deselect=") for c in cmd if c.startswith("--deselect=")]
    assert deselected == row.deselected() and len(deselected) == 2
    assert all("::" in d and "pin" in d for d in deselected)


def test_child_environment_has_no_keys_or_secrets(monkeypatch, tmp_path):
    leaky = (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GH_TOKEN",
        "NEON_PASSWORD",
        "MY_SECRET",
        "DB_PASS",
        "DATABASE_URL",
        "GH_PAT",
        "SERVICE_AUTH",
        "AWS_PROFILE",
        "SSH_AUTH_SOCK",
        "LANGFUSE_SK",
        "NEON_DSN",
        "VIRTUAL_ENV",
    )
    for k in leaky:
        monkeypatch.setenv(k, "x")
    env = dc.child_env(home=tmp_path / "home")
    assert not set(leaky) & set(env), "the child environment is an allowlist"
    assert env["HOME"] == str(tmp_path / "home") != str(Path.home())
    assert env["EVALKIT_DOWNSTREAM"] == "1" and "PATH" in env
    assert set(env) <= dc.ENV_ALLOW | dc.ENV_ALLOW_WINDOWS | {
        "HOME",
        "USERPROFILE",
        "UV_CACHE_DIR",
        "EVALKIT_DOWNSTREAM",
    }


def test_pytest_summary_is_parsed():
    noisy = "2 passed in 0.5s\nretry 3 failed attempts\n"
    assert dc.parse_counts(noisy)["failed"] == 0 and dc.parse_counts(noisy)["passed"] == 2
    line = "== 3 failed, 41 passed, 2 skipped, 1 error in 9.1s =="
    assert dc.parse_counts(line) == {"passed": 41, "failed": 3, "errors": 1, "skipped": 2}
    assert dc.parse_counts("no tests ran in 0.01s") == {
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }


def test_run_one_on_a_tiny_project_records_a_pass(tmp_path):
    """The whole local path on a tiny checkout, with a recording runner (no uv, no network)."""
    proj = tmp_path / "tiny"
    (proj / "tests").mkdir(parents=True)
    (proj / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [project]
            name = "tiny"
            version = "0"
            requires-python = ">=3.11"
            dependencies = []
            """
        ),
        encoding="utf-8",
    )
    (proj / "tests" / "test_ek.py").write_text(
        "from evalkit.scorers import sql_match\n\ndef test_ok():\n    assert sql_match\n",
        encoding="utf-8",
    )
    row = dc.Row("tiny", "me/tiny", "main", "evalkit-harness", "", "auto", "-", "-", "-", "")
    runner = dc.FakeRunner(pytest_line="== 1 passed in 0.01s ==")
    result = dc.run_one(row, proj, ROOT, runner=runner)
    assert result["status"] == "pass"
    assert result["files"] == ["tests/test_ek.py"]
    assert result["counts"]["passed"] == 1
    assert result["audit"]["public"] == 1
    assert [c[:2] for c in runner.calls] == [["uv", "sync"], ["uv", "pip"], ["uv", "run"]]


def test_run_one_marks_failure_and_setup_errors(tmp_path):
    proj = tmp_path / "p"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_x.py").write_text("import evalkit\n", encoding="utf-8")
    row = dc.Row("p", "me/p", "main", "x", "", "auto", "-", "-", "-", "")
    failed = dc.run_one(row, proj, ROOT, runner=dc.FakeRunner("== 1 failed in 0.1s ==", 1))
    assert failed["status"] == "fail" and failed["counts"]["failed"] == 1
    broken = dc.run_one(row, proj, ROOT, runner=dc.FakeRunner("", 0, fail_step=0))
    assert broken["status"] == "setup-error" and "uv sync" in broken["detail"]


def _proj(tmp_path, body="import evalkit\n\ndef test_x():\n    pass\n"):
    proj = tmp_path / "p"
    (proj / "tests").mkdir(parents=True)
    (proj / "tests" / "test_x.py").write_text(body, encoding="utf-8")
    return proj


def test_nothing_collected_or_all_skipped_is_not_a_pass(tmp_path):
    proj = _proj(tmp_path)
    row = dc.Row("p", "me/p", "main", "x", "", "auto", "-", "-", "-", "")
    empty = dc.run_one(row, proj, ROOT, runner=dc.FakeRunner("no tests ran in 0.01s", 5))
    skipped = dc.run_one(row, proj, ROOT, runner=dc.FakeRunner("== 4 skipped in 0.1s ==", 0))
    assert empty["status"] == skipped["status"] == "nothing-ran"


def test_only_known_failures_is_known_drift_and_anything_else_fails(tmp_path):
    proj = _proj(tmp_path, "import evalkit\n\ndef test_x():\n    pass\n\ndef test_y():\n    pass\n")
    known = "tests/test_x.py::test_x"
    row = dc.Row("p", "me/p", "main", "x", "", "auto", "-", "-", known, "")
    out = f"FAILED {known} - AssertionError\n== 1 failed, 3 passed in 0.2s =="
    assert dc.run_one(row, proj, ROOT, runner=dc.FakeRunner(out, 1))["status"] == "known-drift"
    other = "FAILED tests/test_x.py::test_y - boom\n== 1 failed, 3 passed in 0.2s =="
    assert dc.run_one(row, proj, ROOT, runner=dc.FakeRunner(other, 1))["status"] == "fail"


def test_stale_node_ids_are_a_setup_error(tmp_path):
    proj = _proj(tmp_path, "import evalkit\n\nclass TestA:\n    def test_m(self):\n        pass\n")
    ok = dc.Row(
        "p", "me/p", "main", "x", "", "auto", "-", "tests/test_x.py::TestA::test_m", "-", ""
    )
    assert dc.stale_ids(proj, ok.deselected()) == []
    gone = dc.Row("p", "me/p", "main", "x", "", "auto", "-", "tests/test_x.py::test_gone", "-", "")
    res = dc.run_one(gone, proj, ROOT, runner=dc.FakeRunner("== 1 passed in 0.1s =="))
    assert res["status"] == "setup-error" and "test_gone" in res["detail"]


def test_a_checkout_with_a_dotenv_is_refused(tmp_path):
    proj = _proj(tmp_path)
    (proj / ".env").write_text("X=1\n", encoding="utf-8")
    row = dc.Row("p", "me/p", "main", "x", "", "auto", "-", "-", "-", "")
    runner = dc.FakeRunner("== 1 passed in 0.1s ==")
    res = dc.run_one(row, proj, ROOT, runner=runner)
    assert res["status"] == "setup-error" and ".env" in res["detail"] and runner.calls == []


def test_tests_all_runs_the_whole_suite(tmp_path):
    proj = _proj(tmp_path, "def test_x():\n    pass\n")  # reaches EvalKit only via project code
    row = dc.Row("p", "me/p", "main", "x", "", "all", "-", "-", "-", "")
    runner = dc.FakeRunner("== 1 passed in 0.1s ==")
    res = dc.run_one(row, proj, ROOT, runner=runner)
    assert res["status"] == "pass" and res["files"] == []
    assert not any(a.endswith(".py") for a in runner.calls[2])


def test_project_with_no_evalkit_tests_is_reported_not_passed(tmp_path):
    proj = tmp_path / "p"
    (proj / "tests").mkdir(parents=True)
    row = dc.Row("p", "me/p", "main", "x", "", "auto", "-", "-", "-", "")
    result = dc.run_one(row, proj, ROOT, runner=dc.FakeRunner("", 0))
    assert result["status"] == "no-tests"


def test_baseline_run_covers_every_project_with_tests():
    assert BASELINE, "run scripts/downstream_canary.py --all and record the baseline"
    record = json.loads(BASELINE[-1].read_text(encoding="utf-8"))
    auto = {r.project for r in dc.load_registry() if r.tests in {"auto", "all"}}
    assert set(record["projects"]) == auto
    assert re.fullmatch(r"[0-9a-f]{40}", record["evalkit_commit"])
    for name, p in record["projects"].items():
        assert re.fullmatch(r"[0-9a-f]{40}", p["commit"]), name
        assert p["status"] in dc.STATUSES, name
        assert p["files"] or p["status"] == "no-tests" or p["tests"] == "all", name
        if p["status"] != "pass":
            assert p.get("finding"), f"{name} did not pass: record why (D-61)"


def test_scripts_run_standalone():
    import subprocess

    for script in ("downstream_imports.py", "downstream_canary.py"):
        out = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / script), "--self-test"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert out.returncode == 0, out.stderr
