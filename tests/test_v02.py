"""B4.1 (v0.2): case-aware systems under test, task-declared scorers, per-slice reporting, and
backwards compatibility with v0.1 configs, tasks, adapters and results. No network, no spend."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
from typing import Any

import pytest
import yaml

from evalkit import cli
from evalkit import run as R
from evalkit.adapters import (
    CompletionRequest,
    ModelAdapter,
    RawCompletion,
    SystemAdapter,
    discover_systems,
    get_system,
    register_system,
)
from evalkit.datasets import DatasetFileError, load_jsonl_cases
from evalkit.pipeline.generate import (
    Generate,
    GenerateSystem,
    generate_and_validate,
    generate_inputs,
)
from evalkit.runtime import Executor, RunContext, Store
from evalkit.schemas import Case, Interval, Record, RunConfig, RunSummary, SliceSummary, TaskScore
from evalkit.scorers import ScoreResult, scorer
from evalkit.stats import Resamples, interval
from evalkit.tasks import builtin_task
from tests.golden.test_golden_run import reply
from tests.pipeline.fakes import ScriptedBatchAdapter

FAKE_KEY = "sk-test-DO-NOT-PRINT-1234567890"
FIX = Path(__file__).resolve().parent / "fixtures" / "results_v1"
BENCH = ["label_correct", "citation_valid", "groundedness", "coverage", "cost", "latency"]

# ---- a system under test and two task scorers, registered once per process --------------------

CALLS: list[tuple[Case, CompletionRequest]] = []
FAIL: dict[str, BaseException] = {}  # case id -> exception the system raises for it


@register_system("v02_echo")
class EchoSystem(SystemAdapter):
    """Answers from the case itself: label from the reference, first sentence as the answer."""

    version = "1"

    async def answer(self, case: Case, request: CompletionRequest) -> RawCompletion:
        await asyncio.sleep(0)
        CALLS.append((case, request))
        if case.id in FAIL:
            raise FAIL[case.id]
        turns = len(case.meta.get("prior_turns", ()))
        body = {
            "label": case.reference_label or "maybe",
            "answer": case.context[0],
            "citations": [1],
        }
        return RawCompletion(
            model=request.model,
            model_snapshot="echo-1",
            params={},
            raw_text=json.dumps(body),
            tokens_in=10,
            tokens_out=5,
            latency_ms=100 + 10 * turns,
            cost_usd=0.0005,
        )

    def estimate_cost(self, case: Case, request: CompletionRequest) -> float:
        return 0.001


SCORER_CALLS: list[dict] = []


@scorer("v02_answer_words", version="3")
def answer_words(case, completion, **ctx):
    SCORER_CALLS.append({"case": case.id, **ctx})
    if completion.answer is None:
        return ScoreResult(value=0.0, details={"reason": "no_answer"})
    return ScoreResult(value=float(len(completion.answer.answer.split())))


@scorer("v02_is_b1")
def is_b1(case, completion, **ctx):
    return ScoreResult(value=1.0 if case.meta.get("rule") == "B1" else 0.0)


BOOM: dict[str, object] = {}


@scorer("v02_boom")
def boom(case, completion, **ctx) -> Any:
    out = BOOM.get(case.id)
    if isinstance(out, BaseException):
        raise out
    if out is not None:
        return out  # something that is not a ScoreResult
    return ScoreResult(value=1.0)


CONFIG = """models: [course-ta, claude-sonnet-5-5]
systems: {course-ta: v02_echo}
judges: [claude-opus-5-5, gpt-6.1-sol]
extractor: claude-haiku-5-5
seed: 7
"""
V01_CONFIG = """models: [claude-sonnet-5-5, gpt-6.1-sol]
judges: [claude-opus-5-5, gpt-6.1-sol]
extractor: claude-haiku-5-5
seed: 7
thresholds:
  - {metric: headline, op: ">=", value: 0.6}
"""


def rows(n: int = 24) -> list[dict]:
    out = []
    for i in range(n):
        row = {
            "id": f"q{i:03d}",
            "question": f"Does treatment {i} reduce symptoms?",
            "context": [
                f"Treatment {i} reduced symptoms in the trial.",
                f"Side effects of {i} were mild.",
            ],
            "label": ["yes", "no", "maybe"][i % 3],
        }
        if i % 6 != 5:  # every sixth case has no meta at all
            row["meta"] = {
                "rule": "B1" if i % 2 == 0 else "B3",
                "split": "heldout" if i % 4 < 2 else "tuning",
                "prior_turns": [["hi", "hello"]] * (i % 3),
            }
        out.append(row)
    return out


def write_task(tmp: Path, *, scorers=(), slices=(), data=None, name="ta") -> Path:
    data = rows() if data is None else data
    (tmp / "data").mkdir(exist_ok=True)
    (tmp / "data" / f"{name}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in data), encoding="utf-8"
    )
    task = builtin_task("pubmedqa").model_dump()
    task.update(
        {
            "name": name,
            "dataset": f"data/{name}.jsonl",
            "split": "local",
            "scorers": [*BENCH, *scorers],
            "thresholds": [],
        }
    )
    if slices:
        task["slices"] = list(slices)
    path = tmp / f"{name}.yaml"
    path.write_text(yaml.safe_dump(task, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    monkeypatch.setenv("OPENAI_API_KEY", FAKE_KEY)
    fake = ScriptedBatchAdapter(reply)
    monkeypatch.setattr(
        R,
        "make_adapters",
        lambda config, prices: {"anthropic": fake, "openai": fake, **R.system_adapters(config)},
    )
    (tmp_path / "evalkit.yaml").write_text(CONFIG, encoding="utf-8")
    CALLS.clear()
    FAIL.clear()
    SCORER_CALLS.clear()
    BOOM.clear()
    return tmp_path, fake


def run(task: Path, *extra: str, n: int = 24) -> tuple[int, dict]:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(
            [
                "run",
                "evalkit.yaml",
                "--task",
                str(task),
                "--n",
                str(n),
                "--wait",
                "0.02",
                "--json",
                *extra,
            ]
        )
    text = buf.getvalue().strip()
    return code, (json.loads(text) if text.startswith("{") else {})


def files(run_dir: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in run_dir.iterdir() if p.is_file()}


def records_of(run_dir: Path) -> list[Record]:
    return R.read_records(run_dir)


def summary_of(run_dir: Path) -> RunSummary:
    return RunSummary.model_validate_json((run_dir / "summary.json").read_text(encoding="utf-8"))


# ---- SUT-01 system under test -----------------------------------------------------------------


def test_system_adapter_gets_the_whole_case_and_the_rendered_request(env):
    tmp, fake = env
    code, out = run(write_task(tmp))
    assert code in (0, 1), out
    assert len(CALLS) == 24  # once per case, and never through complete()
    by_id = {r["id"]: r for r in rows()}
    for case, req in CALLS:
        row = by_id[case.id]
        assert case.question == row["question"] and case.context == tuple(row["context"])
        assert case.reference_label == row["label"]
        assert json.loads(json.dumps(case.meta)) == row.get("meta", {})
        assert req.model == req.model_id == "course-ta" and req.params == {}
        assert row["question"] in req.prompt and "[1] " + row["context"][0] in req.prompt
        assert req.json_schema is not None  # structured output on by default
    # the generation adapters never saw the system's alias
    assert all(r.model_id != "course-ta" for r in fake.requests)
    # validated, extracted, judged and scored like a model
    recs = [r for r in records_of(tmp / out["run_dir"]) if r.completion.model == "course-ta"]
    assert len(recs) == 24 and all(r.completion.error is None for r in recs)
    assert all(r.completion.model_snapshot == "echo-1" and r.claims for r in recs)
    assert all(r.scores is not None and r.scores.label_correct for r in recs)
    s = summary_of(tmp / out["run_dir"])
    ta = next(m for m in s.models if m.model == "course-ta")
    assert ta.family == "other" and ta.accuracy.point == 1.0 and len(ta.headline) == 2


def test_system_adapter_cannot_be_called_without_a_case():
    sys_ = get_system("v02_echo")
    assert isinstance(sys_, ModelAdapter) and "v02_echo" in discover_systems()
    req = CompletionRequest(model="a", model_id="a", prompt="p")
    with pytest.raises(TypeError, match="case"):
        asyncio.run(sys_.complete(req))
    with pytest.raises(KeyError, match="unknown system"):
        get_system("v02_no_such_system")
    with pytest.raises(ValueError, match="already registered"):
        register_system("v02_echo")(type("Other", (EchoSystem,), {}))


@pytest.mark.parametrize(
    ("config", "needle"),
    [
        (CONFIG.replace("v02_echo", "v02_missing"), "unknown system 'v02_missing'"),
        (
            CONFIG.replace("course-ta, ", "gpt-6-luna, ").replace("{course-ta:", "{gpt-6-luna:"),
            "provider",
        ),
        (
            CONFIG.replace("{course-ta: v02_echo}", "{course-ta: v02_echo, other-ta: v02_echo}"),
            "models",
        ),
    ],
)
def test_systems_config_errors_exit_2_before_any_call(env, capsys, config, needle):
    tmp, fake = env
    (tmp / "evalkit.yaml").write_text(config, encoding="utf-8")
    code, _ = run(write_task(tmp))
    assert code == 2
    assert needle in capsys.readouterr().err
    assert not CALLS and not fake.requests


def test_dry_run_counts_a_system_at_zero_and_needs_no_price(env, capsys):
    tmp, _ = env
    code, out = run(write_task(tmp), "--dry-run")
    assert code == 0 and out["dry_run"] is True
    gen = [ln for ln in out["estimate"]["lines"] if ln["model"] == "course-ta"]
    assert gen and all(ln["usd"] == 0 for ln in gen)
    assert not CALLS


def test_system_answers_are_cached_by_case_and_version(env, monkeypatch):
    tmp, fake = env
    task = write_task(tmp)
    code, out = run(task)
    run_dir = tmp / out["run_dir"]
    before, calls, reqs = files(run_dir), len(CALLS), len(fake.requests)
    assert run(task)[0] == code
    assert len(CALLS) == calls and len(fake.requests) == reqs  # nothing asked twice
    assert files(run_dir) == before  # byte-identical rerun
    # a new adapter version asks every case again
    monkeypatch.setattr(EchoSystem, "version", "2")
    run(task)
    assert len(CALLS) == 2 * calls
    # a changed meta on one case asks only that case again
    monkeypatch.setattr(EchoSystem, "version", "1")
    data = rows()
    data[0]["meta"]["prior_turns"] = [["changed", "turn"]]
    task = write_task(tmp, data=data)
    run(task)
    assert len(CALLS) == 2 * calls + 1 and CALLS[-1][0].id == "q000"


def test_system_cache_key_covers_the_case_and_version():
    node = GenerateSystem({}, {})
    case = Case(id="a", question="q", context=("s",), source="t", meta={"rule": "B1"})
    task = builtin_task("pubmedqa")
    cfg = RunConfig.model_validate(
        {
            "models": ["ta"],
            "systems": {"ta": "v02_echo"},
            "judges": ["claude-opus-5-5"],
            "extractor": "claude-haiku-5-5",
        }
    )
    from evalkit.pipeline.generate import system_inputs

    ctx = RunContext(run_id="r", seed=7)
    (inp,) = system_inputs([case], task, cfg)
    assert inp.case == case and inp.system == "v02_echo" and inp.system_version == "1"
    key = node.cache_key(inp, ctx)
    other = case.model_copy(update={"meta": {"rule": "B3"}})
    assert node.cache_key(system_inputs([other], task, cfg)[0], ctx) != key
    bumped = inp.model_copy(update={"system_version": "2"})
    assert node.cache_key(bumped, ctx) != key


@pytest.mark.safety
def test_system_transient_failures_are_records_and_others_stop_the_run(tmp_path):
    CALLS.clear()
    FAIL.clear()
    cases = [
        Case(id=f"c{i}", question="q?", context=("s one.",), reference_label="yes", source="t")
        for i in range(3)
    ]
    cfg = RunConfig.model_validate(
        {
            "models": ["ta"],
            "systems": {"ta": "v02_echo"},
            "judges": ["claude-opus-5-5"],
            "extractor": "claude-haiku-5-5",
        }
    )
    task = builtin_task("pubmedqa")
    adapters = R.system_adapters(cfg)
    FAIL["c1"] = ConnectionError("down")
    ex = Executor(Store(tmp_path / "s1"))
    out = asyncio.run(generate_and_validate(ex, cases, task, cfg, adapters, {}, run_id="r"))
    assert [g.completion.error for g in out] == [None, "transport", None]
    FAIL["c1"] = ValueError("a bug in the system")
    ex = Executor(Store(tmp_path / "s2"))
    with pytest.raises(ValueError, match="a bug"):
        asyncio.run(generate_and_validate(ex, cases, task, cfg, adapters, {}, run_id="r"))
    # the spend cap holds the system's own estimate per call
    FAIL.clear()
    from evalkit.runtime import BudgetExceeded

    ex = Executor(Store(tmp_path / "s3"), max_cost_usd=0.0015)
    with pytest.raises(BudgetExceeded):
        asyncio.run(generate_and_validate(ex, cases, task, cfg, adapters, {}, run_id="r"))


# ---- SUT-02 Case.meta ---------------------------------------------------------------------------


def test_jsonl_meta_reaches_the_case(tmp_path):
    path = tmp_path / "c.jsonl"
    data = rows(6)
    path.write_text("".join(json.dumps(r) + "\n" for r in data), encoding="utf-8")
    cases = load_jsonl_cases(path, builtin_task("pubmedqa"))
    assert cases[1].meta["rule"] == "B3" and cases[2].meta["prior_turns"] == (("hi", "hello"),) * 2
    assert cases[5].meta == {}


@pytest.mark.parametrize("meta", [[1, 2], "B1", 3, None])
def test_non_object_meta_is_a_dataset_error_naming_the_line(tmp_path, meta):
    path = tmp_path / "c.jsonl"
    data = rows(3)
    data[2]["meta"] = meta
    path.write_text("".join(json.dumps(r) + "\n" for r in data), encoding="utf-8")
    with pytest.raises(DatasetFileError, match=r"c\.jsonl:3: meta must be a JSON object"):
        load_jsonl_cases(path, builtin_task("pubmedqa"))


def test_empty_meta_serialises_exactly_as_v01():
    v01 = (
        '{"id":"q000","question":"Does treatment 0 reduce symptoms?","context":["Treatment 0 '
        'reduced symptoms in the trial.","Side effects of 0 were mild."],"reference_label":"yes",'
        '"source":"local/my-task"}'
    )
    case = Case.model_validate_json(v01)
    assert case.meta == {} and case.model_dump_json() == v01
    assert "meta" not in case.model_dump() and "meta" not in case.model_dump(mode="json")
    with_meta = case.model_copy(update={"meta": {"rule": "B1"}})
    assert json.loads(with_meta.model_dump_json())["meta"] == {"rule": "B1"}
    assert Case.model_validate_json(with_meta.model_dump_json()) == with_meta


@pytest.mark.safety
def test_meta_is_deeply_frozen():
    case = Case(
        id="a",
        question="q",
        context=("s",),
        source="t",
        meta={"prior_turns": [["a", "b"]], "x": {"y": [1]}},
    )
    assert case.meta["prior_turns"] == (("a", "b"),)
    with pytest.raises(TypeError):
        case.meta["rule"] = "B1"  # type: ignore[index]
    with pytest.raises(TypeError):
        case.meta["x"]["y"] = 2  # type: ignore[index]
    with pytest.raises(ValueError):
        Case(id="a", question="q", context=("s",), source="t", meta={"bad": float("nan")})
    hash(case)


# ---- TSC-01 task scorers ------------------------------------------------------------------------


def test_task_scorers_run_once_per_record_with_context(env):
    tmp, _ = env
    code, _ = run(write_task(tmp, scorers=["v02_answer_words"]))
    assert code in (0, 1)
    assert len(SCORER_CALLS) == 48  # 2 models x 24 cases, once each
    assert {c["model"] for c in SCORER_CALLS} == {"course-ta", "claude-sonnet-5-5"}
    one = SCORER_CALLS[0]
    assert {"model", "claims", "verdicts", "scores"} <= set(one)
    assert one["scores"] is not None and isinstance(one["claims"], tuple)
    assert all(
        {v.judge for v in c["verdicts"]} <= {"claude-opus-5-5", "gpt-6.1-sol"} for c in SCORER_CALLS
    )


@pytest.mark.parametrize("name", ["v02_not_registered", "answer-lenght"])
def test_unknown_task_scorer_exits_2_before_any_call(env, capsys, name):
    tmp, fake = env
    code, _ = run(write_task(tmp, scorers=[name]))
    assert code == 2
    err = capsys.readouterr().err
    assert f"unknown scorer {name!r}" in err and "task" in err
    assert not CALLS and not fake.requests


def test_task_scores_reach_records_summary_and_report(env):
    tmp, _ = env
    _, out = run(write_task(tmp, scorers=["v02_answer_words", "v02_is_b1"]))
    run_dir = tmp / out["run_dir"]
    recs = records_of(run_dir)
    for r in recs:
        assert set(r.task_scores) == {"v02_answer_words", "v02_is_b1"}
        ts = r.task_scores["v02_answer_words"]
        assert isinstance(ts, TaskScore) and ts.version == "3"
        words = len(r.completion.answer.answer.split()) if r.completion.answer else 0
        assert ts.value == float(words)
    raw = json.loads((run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert raw["task_scores"]["v02_answer_words"]["version"] == "3"
    s = summary_of(run_dir)
    for m in s.models:
        mine = sorted((r for r in recs if r.completion.model == m.model), key=lambda r: r.case_id)
        vals = [r.task_scores["v02_is_b1"].value for r in mine]
        iv = m.task_scores["v02_is_b1"]
        assert iv.point == pytest.approx(math.fsum(vals) / len(vals))
        assert iv.lo <= iv.point <= iv.hi
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "v02_answer_words" in html and "v02_is_b1" in html


def test_rerun_with_task_scorers_is_byte_identical_and_calls_nothing(env):
    tmp, fake = env
    task = write_task(tmp, scorers=["v02_answer_words"], slices=["rule"])
    _, out = run(task)
    run_dir = tmp / out["run_dir"]
    before, calls, reqs = files(run_dir), len(CALLS), len(fake.requests)
    run(task)
    assert files(run_dir) == before and len(CALLS) == calls and len(fake.requests) == reqs
    # `evalkit report` re-renders the same report from the folder
    report = (run_dir / "report.html").read_bytes()
    assert cli.main(["report", str(run_dir)]) == 0
    assert (run_dir / "report.html").read_bytes() == report


@pytest.mark.safety
@pytest.mark.parametrize(
    ("bad", "needle"),
    [
        (RuntimeError("scorer bug"), "scorer bug"),
        ("not a ScoreResult", "ScoreResult"),
    ],
)
def test_a_raising_or_bad_task_scorer_stops_the_run_naming_it(env, capsys, bad, needle):
    tmp, _ = env
    BOOM["q003"] = bad
    code, _ = run(write_task(tmp, scorers=["v02_boom"]))
    assert code == 2
    err = capsys.readouterr().err
    assert "v02_boom" in err and "q003" in err and needle in err
    with pytest.raises(ValueError):
        ScoreResult(value=float("inf"))
    with pytest.raises(ValueError):
        TaskScore(value=float("nan"), version="1")


# ---- SLC-01 slices ------------------------------------------------------------------------------


def test_slice_intervals_match_an_independent_computation(env):
    tmp, _ = env
    _, out = run(write_task(tmp, scorers=["v02_answer_words"], slices=["rule", "split"]))
    run_dir = tmp / out["run_dir"]
    s, recs = summary_of(run_dir), records_of(run_dir)
    cases = {r["id"]: r for r in rows()}
    assert {(x.field, x.value) for x in s.slices} == {
        ("rule", "B1"),
        ("rule", "B3"),
        ("rule", "(none)"),
        ("split", "heldout"),
        ("split", "tuning"),
        ("split", "(none)"),
    }
    assert len(s.slices) == 6 * 2  # one per field, value and model
    for x in s.slices:
        mine = sorted(
            (
                r
                for r in recs
                if r.completion.model == x.model
                and str(cases[r.case_id].get("meta", {}).get(x.field, "(none)")) == x.value
            ),
            key=lambda r: r.case_id,
        )
        assert x.n == len(mine) > 0
        rs = Resamples(len(mine), 10_000, seed=7)
        sc = {r.case_id: r.scores for r in mine}
        assert all(s is not None for s in sc.values())
        acc = [float(s.label_correct) for s in sc.values() if s is not None]
        assert x.accuracy == interval(acc, rs)
        for j in ("claude-opus-5-5", "gpt-6.1-sol"):
            head = [float(s.correct_and_grounded[j]) for s in sc.values() if s is not None]
            assert x.headline[j] == interval(head, rs)
        words = [r.task_scores["v02_answer_words"].value for r in mine]
        assert x.task_scores["v02_answer_words"] == interval(words, rs)
        cost = math.fsum(r.completion.cost_usd for r in mine)
        assert x.cost_per_100_usd == pytest.approx(cost / len(mine) * 100)
    # the B1 slice of the system holds exactly the even-numbered cases with meta
    b1 = next(x for x in s.slices if (x.field, x.value, x.model) == ("rule", "B1", "course-ta"))
    assert b1.n == sum(1 for i in range(24) if i % 6 != 5 and i % 2 == 0)


def test_slice_summary_schema_rules():
    iv = Interval(point=0.5, lo=0.4, hi=0.6)
    base: dict[str, Any] = dict(
        field="rule",
        value="B1",
        model="m",
        n=4,
        n_errors={},
        accuracy=iv,
        headline={"j": iv},
        groundedness_mean={"j": iv},
        citation_validity=iv,
        coverage=iv,
        cost_per_100_usd=0.1,
        latency_p50_ms=1,
        latency_p95_ms=2,
    )
    SliceSummary(**base)
    with pytest.raises(ValueError, match="same judges"):
        SliceSummary(**{**base, "groundedness_mean": {}})
    with pytest.raises(ValueError, match="n_errors"):
        SliceSummary(**{**base, "n_errors": {"schema": 5}})
    summary = json.loads((FIX / "run_summary.json").read_text(encoding="utf-8"))
    model = summary["models"][0]["model"]
    judges = [summary["models"][0]["headline"]]
    pending = {
        **summary,
        "judging_status": "pending",
        "finished_at": None,
        "self_preference": {},
        "comparisons": [],
        "judges": [],
        "calibration_status": "uncalibrated",
        "models": [{**m, "headline": {}, "groundedness_mean": {}} for m in summary["models"]],
    }
    one = {
        **base,
        "model": model,
        "headline": judges[0],
        "groundedness_mean": dict.fromkeys(judges[0], iv.model_dump()),
    }
    with pytest.raises(ValueError, match="pending"):
        RunSummary.model_validate({**pending, "slices": [one]})
    with pytest.raises(ValueError, match="unknown model"):
        RunSummary.model_validate({**summary, "slices": [{**one, "model": "nobody"}]})


@pytest.mark.golden
def test_slices_reach_summary_and_report_and_rerender_identically(env):
    tmp, _ = env
    _, out = run(write_task(tmp, slices=["split"]))
    run_dir = tmp / out["run_dir"]
    manifest = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert manifest["slices"] == ["split"]
    raw = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert {x["value"] for x in raw["slices"]} == {"heldout", "tuning", "(none)"}
    html = (run_dir / "report.html").read_text(encoding="utf-8")
    assert "<h2 id=slices-h>Slices</h2>" in html and "heldout" in html
    report = (run_dir / "report.html").read_bytes()
    assert cli.main(["report", str(run_dir)]) == 0
    assert (run_dir / "report.html").read_bytes() == report


@pytest.mark.safety
def test_non_scalar_slice_values_are_refused_and_names_are_escaped(env, capsys):
    tmp, _ = env
    data = rows()
    data[0]["meta"]["rule"] = ["B1", "B2"]
    code, _ = run(write_task(tmp, slices=["rule"], data=data))
    assert code == 2 and "slice 'rule'" in capsys.readouterr().err
    # names and values reach the report escaped
    data = rows()
    for r in data:
        if "meta" in r:
            r["meta"]["<b>x</b>"] = "<script>alert(1)</script>"
    code, out = run(write_task(tmp, slices=["<b>x</b>"], data=data, name="esc"))
    assert code in (0, 1)
    html = (tmp / out["run_dir"] / "report.html").read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html
    assert "<b>x</b>" not in html


def test_slice_values_from_top_level_fields_and_scalars():
    from evalkit.pipeline.aggregate import slice_value

    c = Case(
        id="a",
        question="q",
        context=("s",),
        reference_label="yes",
        source="t",
        meta={"n": 3, "flag": True, "none": None},
    )
    assert slice_value(c, "reference_label") == "yes"
    assert slice_value(c, "source") == "t"
    assert slice_value(c, "n") == "3" and slice_value(c, "flag") == "true"
    assert slice_value(c, "none") == "(none)" and slice_value(c, "missing") == "(none)"
    with pytest.raises(ValueError, match="slice 'x'"):
        slice_value(c.model_copy(update={"meta": {"x": {"a": 1}}}), "x")


# ---- BWC-01 backwards compatibility -------------------------------------------------------------

# Values computed with evalkit 0.1.0 (git ca22b5c) for the same inputs: v0.2 must not move them,
# or every v0.1 run folder would stop resuming and every cached call would be paid again.
V01_RUN_ID = "r-5f631b49fbce"
V01_CONFIG_HASH = "9b2d6f107aaa793e"
V01_GENERATE_KEY = "b208c6f727bc20e80e6bffe4f2a29605fcd768162c0d80f514a51f595822d228"
V01_TASK_SHA = "512e90d170c2fd4897fb1e2125b51689a2c3655f2d94a2d82e9c3f07c3329215"


def _v01_plan(tmp: Path) -> R.Plan:
    (tmp / "evalkit.yaml").write_text(V01_CONFIG, encoding="utf-8")
    data = [{k: v for k, v in r.items() if k != "meta"} for r in rows(12)]
    (tmp / "cases.jsonl").write_text("".join(json.dumps(r) + "\n" for r in data), encoding="utf-8")
    task = builtin_task("pubmedqa").model_dump()
    task.update({"name": "my-task", "dataset": "cases.jsonl", "split": "local"})
    (tmp / "t.yaml").write_text(yaml.safe_dump(task, sort_keys=False), encoding="utf-8")
    return R.plan_run(tmp / "evalkit.yaml", task=str(tmp / "t.yaml"), n=12, out=tmp / "runs")


def test_v01_run_ids_and_config_hash_are_unchanged(tmp_path):
    from evalkit.runtime.canonical import sha256_hex

    plan = _v01_plan(tmp_path)
    assert plan.run_id == V01_RUN_ID and plan.config_hash == V01_CONFIG_HASH
    assert sha256_hex(plan.task) == V01_TASK_SHA
    assert "systems" not in plan.config.model_dump() and "slices" not in plan.task.model_dump()


def test_v01_cache_keys_are_unchanged(tmp_path):
    plan = _v01_plan(tmp_path)
    inp = generate_inputs(plan.cases[:1], plan.task, plan.config)[0]
    assert Generate({}, {}).cache_key(inp, RunContext(run_id="x", seed=7)) == V01_GENERATE_KEY


V01_DUMPS = {  # sha256 of model_dump_json() under evalkit 0.1.0
    "record.json": "57882b298d88ccd62406a0500311ac6257f3aa92879dad4d9a64022f983b5127",
    "run_summary.json": "16eba540bbe2ddcc56c0542fd028c369ffdb39e5e48999bab7cf0dd109c1e231",
    "run_config.json": "06876e129fd7028a0fd6dcf73beab06d93ec77dad2e5746584ebafd4284a204a",
}


def test_v01_results_fixtures_load_and_dump_unchanged():
    import hashlib

    models = {"record.json": Record, "run_summary.json": RunSummary, "run_config.json": RunConfig}
    for name, model in models.items():
        obj = model.model_validate_json((FIX / name).read_text(encoding="utf-8"))
        digest = hashlib.sha256(obj.model_dump_json().encode()).hexdigest()
        assert digest == V01_DUMPS[name], f"{name} serialises differently from v0.1"
    cfg = RunConfig.model_validate_json((FIX / "run_config.json").read_text(encoding="utf-8"))
    assert cfg.systems == {}


def test_new_fields_are_omitted_when_empty():
    rec = Record.model_validate_json((FIX / "record.json").read_text(encoding="utf-8"))
    assert rec.task_scores == {} and "task_scores" not in rec.model_dump(mode="json")
    s = RunSummary.model_validate_json((FIX / "run_summary.json").read_text(encoding="utf-8"))
    dumped = s.model_dump(mode="json")
    assert "slices" not in dumped and all("task_scores" not in m for m in dumped["models"])
    task = builtin_task("pubmedqa")
    assert task.slices == () and "slices" not in task.model_dump()
    with_ts = rec.model_copy(update={"task_scores": {"x": TaskScore(value=1.0, version="1")}})
    assert Record.model_validate_json(with_ts.model_dump_json()) == with_ts


def test_v01_style_plugins_still_work():
    """A v0.1 ModelAdapter subclass (complete only) and a v0.1 @scorer are untouched."""

    class V01Adapter(ModelAdapter):
        provider = "v01"

        async def complete(self, req):
            return RawCompletion(
                model=req.model,
                model_snapshot="s",
                params={},
                raw_text="{}",
                tokens_in=0,
                tokens_out=0,
                latency_ms=0,
                cost_usd=0.0,
            )

        async def submit_batch(self, reqs):
            raise NotImplementedError

        async def collect_batch(self, batch_id, reqs):
            raise NotImplementedError

    req = CompletionRequest(model="m", model_id="m", prompt="p")
    assert asyncio.run(V01Adapter().complete(req)).raw_text == "{}"
    assert "case" not in CompletionRequest.model_fields

    @scorer("v02_compat_v01_scorer")
    def v01(case, completion, **ctx):
        return ScoreResult(value=1.0)

    from evalkit.scorers import get_scorer

    assert get_scorer("v02_compat_v01_scorer") is v01


def test_version_is_0_2_0_everywhere():
    import tomllib

    import evalkit

    root = Path(__file__).resolve().parents[1]
    proj = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert proj["version"] == evalkit.__version__ == "0.2.0"
    lock = (root / "uv.lock").read_text(encoding="utf-8")
    assert 'name = "evalkit-harness"\nversion = "0.2.0"' in lock
    log = (root / "changelog.md").read_text(encoding="utf-8")
    assert "[B4.1]" in log and "0.2.0" in log
