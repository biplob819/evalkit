import json
import threading
import urllib.request

import pytest

from evalkit import cli
from evalkit import run as R
from evalkit.datasets.pubmedqa import parse_pqa_l
from evalkit.tasks import builtin_task
from tests.golden.test_golden_run import MINI, reply
from tests.pipeline.fakes import ScriptedBatchAdapter

FAKE_KEY = "sk-test-DO-NOT-PRINT-1234567890"
CONFIG = """models: [claude-sonnet-5-5, gpt-6.1-sol]
judges: [claude-opus-5-5, gpt-6.1-sol]
extractor: claude-haiku-5-5
seed: 7
thresholds:
  - {metric: headline, op: ">=", value: 0.6}
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A temp project: fake keys, fake adapters, the 12-case fixture as PubMedQA."""
    monkeypatch.chdir(tmp_path)  # never read the repo's real .env
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    rows = json.loads(MINI.read_text(encoding="utf-8"))
    monkeypatch.setattr(R, "load_pqa_l", lambda: parse_pqa_l(rows))
    fake = ScriptedBatchAdapter(reply)
    monkeypatch.setattr(
        R, "make_adapters", lambda config, prices: {"anthropic": fake, "openai": fake}
    )
    (tmp_path / "evalkit.yaml").write_text(CONFIG, encoding="utf-8")
    return tmp_path, fake


def run(*argv):
    return cli.main(["run", "evalkit.yaml", "--n", "12", "--wait", "0.02", *argv])


def files(run_dir):
    return {p.name: p.read_bytes() for p in run_dir.iterdir() if p.is_file()}


# ---- CLI-01 run, resume, dry run, json, exit codes ---------------------------------------------


def test_run_writes_the_run_folder_and_resumes_for_free(env, capsys):
    tmp, fake = env
    code = run("--json")
    out = json.loads(capsys.readouterr().out)
    assert code == 1 and out["exit_code"] == 1  # the 60% headline bar fails on the fixture
    run_dir = tmp / out["run_dir"]
    assert {
        "run.json",
        "cases.jsonl",
        "records.jsonl",
        "summary.json",
        "report.html",
        "charts.vl.json",
    } <= set(files(run_dir))
    before, calls, batches = files(run_dir), len(fake.requests), len(fake.submitted)
    assert run("--json") == 1
    assert len(fake.requests) == calls and len(fake.submitted) == batches  # all cached
    assert files(run_dir) == before  # byte-identical, timestamps persisted


def test_dry_run_makes_no_calls_and_prints_the_estimate(env, capsys):
    _tmp, fake = env
    assert run("--dry-run") == 0
    out = capsys.readouterr().out
    assert "generate" in out and "judge" in out and "no API calls" in out
    assert fake.requests == [] and fake.submitted == []
    assert run("--dry-run", "--json") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] and payload["estimate"]["total_usd"] > 0


def test_cost_cap_exits_3_with_a_resume_hint(env, capsys):
    assert run("--max-cost", "0.000001") == 3
    out = capsys.readouterr().out
    assert "cost cap" in out and "--max-cost" in out


def test_pending_judges_exit_4_and_resume_collects(env, capsys, monkeypatch):
    tmp, _ = env
    slow = ScriptedBatchAdapter(reply, pending_polls=10**6)
    monkeypatch.setattr(
        R, "make_adapters", lambda config, prices: {"anthropic": slow, "openai": slow}
    )
    assert run("--wait", "0", "--json") == 4
    out = json.loads(capsys.readouterr().out)
    assert out["judging_status"] == "pending"
    summary = json.loads((tmp / out["run_dir"] / "summary.json").read_text(encoding="utf-8"))
    assert summary["judging_status"] == "pending"
    slow.pending_polls = 0  # batches finish
    assert run("--json") in (0, 1)
    assert json.loads(capsys.readouterr().out)["judging_status"] == "complete"


def test_live_judges_share_the_batch_cache(env, capsys):
    _tmp, fake = env
    assert run("--live-judges", "--json") == 1
    capsys.readouterr()
    assert fake.submitted == []  # no batches
    assert run("--json") == 1  # batch mode now: everything already judged
    assert fake.submitted == []


# ---- CLI-04 validation ------------------------------------------------------------------------


@pytest.mark.safety
@pytest.mark.parametrize(
    ("setup", "argv", "needle"),
    [
        (None, ["run", "missing.yaml"], "config file not found"),
        ("models: [a\n", ["run", "evalkit.yaml"], "invalid config"),
        (
            CONFIG.replace("claude-sonnet-5-5", "mystery-model"),
            ["run", "evalkit.yaml", "--n", "12"],
            "unknown model 'mystery-model'",
        ),
        (
            CONFIG.replace("metric: headline", "metric: vibes"),
            ["run", "evalkit.yaml"],
            "invalid config",
        ),
        (CONFIG, ["run", "evalkit.yaml", "--n", "999"], "--n must be between 1 and 12"),
        (CONFIG, ["run", "evalkit.yaml", "--task", "nope"], "unknown built-in task"),
    ],
)
def test_invalid_inputs_exit_2_before_any_call(env, capsys, setup, argv, needle):
    tmp, fake = env
    if setup is not None:
        (tmp / "evalkit.yaml").write_text(setup, encoding="utf-8")
    assert cli.main(argv) == 2
    err = capsys.readouterr().err
    assert needle in err and FAKE_KEY not in err
    assert fake.requests == []


@pytest.mark.safety
def test_missing_keys_are_named_never_printed(env, capsys, monkeypatch):
    _, fake = env
    monkeypatch.delenv("OPENAI_API_KEY")
    assert run() == 2
    err = capsys.readouterr().err
    assert "OPENAI_API_KEY" in err and FAKE_KEY not in err and fake.requests == []


def test_one_judge_key_runs_single_judge(env, capsys, monkeypatch):
    tmp, _ = env
    (tmp / "evalkit.yaml").write_text(
        CONFIG.replace("[claude-opus-5-5, gpt-6.1-sol]", "[claude-opus-5-5, gpt-6-luna]").replace(
            "models: [claude-sonnet-5-5, gpt-6.1-sol]", "models: [claude-sonnet-5-5]"
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("OPENAI_API_KEY")
    assert run("--json") in (0, 1)
    assert json.loads(capsys.readouterr().out)["judge_mode"] == "single"


def test_unknown_flag_exits_2(env):
    with pytest.raises(SystemExit) as e:
        cli.main(["run", "evalkit.yaml", "--bogus"])
    assert e.value.code == 2


# ---- CLI-03 report ----------------------------------------------------------------------------


def test_report_rerenders_identically_and_publish_is_guarded(env, capsys):
    tmp, _ = env
    run("--json")
    run_dir = tmp / json.loads(capsys.readouterr().out)["run_dir"]
    before = (run_dir / "report.html").read_bytes()
    assert cli.main(["report", str(run_dir)]) == 0
    assert (run_dir / "report.html").read_bytes() == before
    assert cli.main(["report", str(run_dir), "--publish"]) == 2
    assert "calibration" in capsys.readouterr().err


# ---- CLI-05 user task, and CLI-02 calibration on it --------------------------------------------


def write_user_task(tmp, n=60):
    lines = []
    for i in range(n):
        lines.append(
            json.dumps(
                {
                    "id": f"q{i:03d}",
                    "question": f"Does treatment {i} reduce symptoms?",
                    "context": [
                        f"Treatment {i} reduced symptoms in the trial.",
                        f"Side effects of {i} were mild.",
                    ],
                    "label": ["yes", "no", "maybe"][i % 3],
                }
            )
        )
    (tmp / "data").mkdir()
    (tmp / "data" / "cases.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    task = builtin_task("pubmedqa").model_dump()
    task.update({"name": "my-task", "dataset": "data/cases.jsonl", "split": "local"})
    import yaml

    (tmp / "my_task.yaml").write_text(yaml.safe_dump(task, sort_keys=False), encoding="utf-8")
    return tmp / "my_task.yaml"


def test_user_yaml_task_runs_end_to_end(env, capsys):
    tmp, _ = env
    write_user_task(tmp)
    code = cli.main(
        ["run", "evalkit.yaml", "--task", "my_task.yaml", "--n", "60", "--wait", "0.02", "--json"]
    )
    out = json.loads(capsys.readouterr().out)
    assert code in (0, 1)
    summary = json.loads((tmp / out["run_dir"] / "summary.json").read_text(encoding="utf-8"))
    assert summary["task"] == "my-task" and all(m["n"] == 60 for m in summary["models"])


def test_calibrate_export_label_score_then_report_is_calibrated(env, capsys):
    tmp, _ = env
    write_user_task(tmp)
    cli.main(
        ["run", "evalkit.yaml", "--task", "my_task.yaml", "--n", "60", "--wait", "0.02", "--json"]
    )
    run_dir = tmp / json.loads(capsys.readouterr().out)["run_dir"]
    assert cli.main(["calibrate", "export", str(run_dir), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["claims"] >= 100
    assert cli.main(["calibrate", "export", str(run_dir)]) == 2  # refuses to clobber the sheet
    capsys.readouterr()
    # Label through the page: one real HTTP post, the rest through the same state object.
    server = R.cal_labeller(run_dir)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        item = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{port}/api/item").read())
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/label",
            data=json.dumps(
                {"key": item["key"], "label": "supported", "extraction_error": False}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req).read()
        records = {r.case_id + ":" + r.completion.model: r for r in R.read_records(run_dir)}
        for row in server.labeller.items:
            if row.key == item["key"]:
                continue
            rec = records[row.claim_id.rsplit(":", 1)[0]]
            v = next(
                (
                    v
                    for v in rec.verdicts
                    if v.claim_id == row.claim_id and v.judge == "claude-opus-5-5"
                ),
                None,
            )
            server.labeller.record(row.key, v.verdict if v else "unsupported", False)
    finally:
        server.shutdown()
        server.server_close()
    assert cli.main(["calibrate", "score", str(run_dir), "--json"]) == 0
    kappa = json.loads(capsys.readouterr().out)["kappa"]
    assert set(kappa) == {"claude-opus-5-5", "gpt-6.1-sol"}
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["calibration_status"] == "calibrated"
    assert "Judges not calibrated" not in (run_dir / "report.html").read_text(encoding="utf-8")
    assert cli.main(["report", str(run_dir), "--publish"]) == 0
    # A rubric change must not reuse this held-out set.
    cal = json.loads((run_dir / "calibration" / "calibration.json").read_text(encoding="utf-8"))
    for entry in cal["history"]:
        entry["judge_prompt_hash"] = "0" * 16
        for a in entry["agreements"]:
            a["judge_prompt_hash"] = "0" * 16
    cal["current"] = cal["history"][-1]
    (run_dir / "calibration" / "calibration.json").write_text(json.dumps(cal), encoding="utf-8")
    assert cli.main(["calibrate", "score", str(run_dir)]) == 2
    assert "fresh held-out" in capsys.readouterr().err


# ---- B2.2 verifier regressions (L-29) ---------------------------------------------------------


class AuthError(Exception):
    status_code = 401


@pytest.mark.safety
def test_provider_auth_error_exits_2_with_masked_message_and_json(env, capsys, monkeypatch):
    def boom(req):
        return AuthError(f"invalid x-api-key {FAKE_KEY}")

    bad = ScriptedBatchAdapter(boom)
    monkeypatch.setattr(
        R, "make_adapters", lambda config, prices: {"anthropic": bad, "openai": bad}
    )
    assert run("--json") == 2
    cap = capsys.readouterr()
    assert FAKE_KEY not in cap.out + cap.err
    payload = json.loads(cap.out)
    assert payload["exit_code"] == 2 and "rejected the API key" in payload["error"]


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (["--max-cost", "-1"], "--max-cost must be 0 or more"),
        (["--wait", "-5"], "--wait must be 0"),
    ],
)
def test_negative_cap_or_wait_exit_2(env, capsys, args, needle):
    _, fake = env
    assert run(*args) == 2
    assert needle in capsys.readouterr().err and fake.requests == []


def test_bad_endpoint_url_exits_2(env, capsys):
    tmp, _ = env
    (tmp / "evalkit.yaml").write_text(
        CONFIG.replace("claude-sonnet-5-5, gpt-6.1-sol]", "claude-sonnet-5-5, my-llama]")
        + "endpoints: {my-llama: localhost:8000}\n",
        encoding="utf-8",
    )
    assert run() == 2 and "http(s) URL" in capsys.readouterr().err


def test_max_cost_zero_is_a_zero_cap_not_no_cap(env, capsys):
    _, fake = env
    assert run("--max-cost", "0") == 3
    assert fake.requests == []


def test_cost_cap_is_a_total_across_resumes(env, capsys):
    first = run("--max-cost", "0.05", "--json")
    out = json.loads(capsys.readouterr().out)
    assert first == 3 and 0 < out["spent_usd_total"] <= 0.05
    again = run("--max-cost", "0.05", "--json")
    out2 = json.loads(capsys.readouterr().out)
    # A retry with the same cap gets no fresh budget: the folder's total stays under it.
    assert again == 3 and out["spent_usd_total"] <= out2["spent_usd_total"] <= 0.05


def test_live_judges_never_pay_for_claims_in_open_batches(env, capsys, monkeypatch):
    slow = ScriptedBatchAdapter(reply, pending_polls=10**6)
    monkeypatch.setattr(
        R, "make_adapters", lambda config, prices: {"anthropic": slow, "openai": slow}
    )
    assert run("--wait", "0") == 4
    capsys.readouterr()
    judge_calls_before = sum(1 for r in slow.requests if r.model == "judge")
    assert run("--live-judges", "--wait", "0") == 4
    assert "without --live-judges" in capsys.readouterr().out
    assert sum(1 for r in slow.requests if r.model == "judge") == judge_calls_before


def test_dry_run_needs_no_keys(env, capsys, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    monkeypatch.delenv("OPENAI_API_KEY")
    assert run("--dry-run") == 0 and "no API calls" in capsys.readouterr().out


def test_rerun_with_a_missing_judge_key_refuses_to_downgrade(env, capsys, monkeypatch):
    tmp, _ = env
    anthropic_models = CONFIG.replace(
        "models: [claude-sonnet-5-5, gpt-6.1-sol]", "models: [claude-sonnet-5-5]"
    )
    (tmp / "evalkit.yaml").write_text(anthropic_models, encoding="utf-8")
    assert run("--json") in (0, 1)
    assert json.loads(capsys.readouterr().out)["judge_mode"] == "dual"
    monkeypatch.delenv("OPENAI_API_KEY")  # the second judge is no longer usable
    assert run("--json") == 2
    assert "judged by" in capsys.readouterr().err


def test_export_force_refuses_while_labels_exist_and_import_refuses_overwrite(env, capsys):
    tmp, _ = env
    write_user_task(tmp)
    cli.main(
        ["run", "evalkit.yaml", "--task", "my_task.yaml", "--n", "60", "--wait", "0.02", "--json"]
    )
    run_dir = tmp / json.loads(capsys.readouterr().out)["run_dir"]
    assert cli.main(["calibrate", "export", str(run_dir)]) == 0
    csv_path = run_dir / "calibration" / "sheet.csv"
    import csv as _csv
    import io

    rows = list(_csv.DictReader(io.StringIO(csv_path.read_text(encoding="utf-8"))))
    for r in rows:
        r["label"] = "S"
    buf = io.StringIO()
    w = _csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    filled = tmp / "filled.csv"
    filled.write_text(buf.getvalue(), encoding="utf-8")
    assert cli.main(["calibrate", "import", str(run_dir), str(filled)]) == 0
    assert cli.main(["calibrate", "import", str(run_dir), str(filled)]) == 2  # no overwrite
    assert cli.main(["calibrate", "export", str(run_dir), "--force"]) == 2  # labels exist
    assert "Move the calibration folder aside" in capsys.readouterr().err


def test_export_refuses_while_judging_is_pending(env, capsys, monkeypatch):
    slow = ScriptedBatchAdapter(reply, pending_polls=10**6)
    monkeypatch.setattr(
        R, "make_adapters", lambda config, prices: {"anthropic": slow, "openai": slow}
    )
    run("--wait", "0", "--json")
    run_dir = env[0] / json.loads(capsys.readouterr().out)["run_dir"]
    assert cli.main(["calibrate", "export", str(run_dir)]) == 2
    assert "pending" in capsys.readouterr().err


def test_dotenv_is_read_from_the_current_folder(env, capsys, monkeypatch):
    tmp, _ = env
    monkeypatch.delenv("OPENAI_API_KEY")
    (tmp / ".env").write_text(f"OPENAI_API_KEY={FAKE_KEY}\n", encoding="utf-8")
    assert run("--json") in (0, 1)
    assert FAKE_KEY not in capsys.readouterr().out


def test_config_thresholds_override_task_thresholds(env, capsys):
    tmp, _ = env
    (tmp / "evalkit.yaml").write_text(CONFIG.replace("value: 0.6", "value: 0.0"), encoding="utf-8")
    assert run("--json") == 0  # a 0% bar from the config replaces the task's 60%


def test_task_dataset_path_is_relative_to_the_task_file(env, capsys, monkeypatch):
    tmp, _ = env
    write_user_task(tmp)
    other = tmp / "elsewhere"
    other.mkdir()
    (other / "evalkit.yaml").write_text(CONFIG, encoding="utf-8")
    monkeypatch.chdir(other)
    code = cli.main(
        [
            "run",
            "evalkit.yaml",
            "--task",
            str(tmp / "my_task.yaml"),
            "--n",
            "60",
            "--wait",
            "0.02",
            "--json",
        ]
    )
    assert code in (0, 1)


@pytest.mark.parametrize(
    ("row", "needle"),
    [
        ({"id": "a:b", "question": "q", "context": ["s"]}, "without ':'"),
        ({"id": None, "question": "q", "context": ["s"]}, "id must be"),
        ({"id": "x", "question": "", "context": ["s"]}, "question must be"),
        ({"id": "x", "question": "q", "context": []}, "context must be"),
        ({"id": "x", "question": "q", "context": ["s"], "label": "perhaps"}, "not one of the task"),
        ({"question": "q", "context": ["s"]}, "missing id"),
    ],
)
def test_local_dataset_rows_are_validated(tmp_path, row, needle):
    from evalkit.datasets import DatasetFileError, load_jsonl_cases
    from evalkit.tasks import builtin_task as bt

    path = tmp_path / "d.jsonl"
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    with pytest.raises(DatasetFileError, match=needle):
        load_jsonl_cases(path, bt("pubmedqa"))
    dup = tmp_path / "dup.jsonl"
    ok = {"id": "x", "question": "q", "context": ["s"]}
    dup.write_text(json.dumps(ok) + "\n" + json.dumps(ok) + "\n", encoding="utf-8")
    with pytest.raises(DatasetFileError, match="duplicate id"):
        load_jsonl_cases(dup, bt("pubmedqa"))


def test_live_judging_respects_concurrency_and_retries_parse_failures(env, capsys, monkeypatch):
    import asyncio

    tmp, _ = env
    (tmp / "evalkit.yaml").write_text(
        CONFIG + "concurrency: {anthropic: 2, openai: 2}\n", encoding="utf-8"
    )
    state = {"garbage_left": 3}
    live: dict[str, int] = {}
    peak: dict[str, int] = {}

    class Counting(ScriptedBatchAdapter):
        async def complete(self, req):
            if req.model != "judge":
                return await super().complete(req)
            j = req.model_id  # one judge per provider here
            live[j] = live.get(j, 0) + 1
            peak[j] = max(peak.get(j, 0), live[j])
            await asyncio.sleep(0.001)
            live[j] -= 1
            if state["garbage_left"] > 0:
                state["garbage_left"] -= 1
                self.requests.append(req)
                return (await super().complete(req)).model_copy(update={"raw_text": "garbage"})
            return await super().complete(req)

    fake = Counting(reply)
    monkeypatch.setattr(
        R, "make_adapters", lambda config, prices: {"anthropic": fake, "openai": fake}
    )
    assert run("--live-judges", "--json") in (0, 1)
    assert peak and max(peak.values()) <= 2 and state["garbage_left"] == 0  # per provider
    summary = json.loads(capsys.readouterr().out)
    assert summary["judging_status"] == "complete"
