from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from evalkit.pipeline.aggregate import (
    aggregate,
    score_record,
    stamp_lines,
    summary_json,
    write_summary,
)
from evalkit.pipeline.extract import ClaimSet
from evalkit.pipeline.judge import JudgeSelection, JudgingResult
from evalkit.schemas import (
    Answer,
    Case,
    Claim,
    ClaimVerdict,
    Completion,
    Interval,
    JudgeAgreement,
    Record,
    RunConfig,
    RunSummary,
    Scores,
)
from evalkit.schemas.benchmark import ErrorType
from evalkit.schemas.config import Threshold

J1, J2 = "claude-opus-5-5", "gpt-6.1-sol"
T0 = datetime(2026, 11, 14, 12, tzinfo=UTC)
T1 = datetime(2026, 11, 14, 13, tzinfo=UTC)
FAM = {"sonnet": "anthropic", "haiku": "anthropic", "sol": "openai", "luna": "openai"}


def comp(
    model: str, *, error: ErrorType | None = None, cost=0.01, latency=1000, label="yes"
) -> Completion:
    return Completion(
        model=model,
        model_snapshot=model + "-snap",
        params={"effort": "medium"},
        raw_text="{}",
        answer=None
        if error in ("schema", "transport")
        else Answer(label=label, answer="a", citations=(1,)),
        tokens_in=10,
        tokens_out=5,
        latency_ms=latency,
        cost_usd=cost,
        error=error,
    )


def rec(
    model,
    case_id,
    *,
    correct,
    grounded=(True, True),
    error=None,
    judges=(J1, J2),
    cost=0.01,
    latency=1000,
):
    g = dict(zip(judges, grounded, strict=False))
    scores = Scores(
        label_correct=correct and error is None,
        citation_valid=0.0 if error else 1.0,
        groundedness={j: 1.0 if (v and not error) else 0.0 for j, v in g.items()},
        fully_grounded={j: bool(v and not error) for j, v in g.items()},
        coverage=0.0 if error else 1.0,
        correct_and_grounded={j: bool(correct and v and not error) for j, v in g.items()},
    )
    return Record(
        schema_version="1.0",
        run_id="r",
        task="pubmedqa",
        case_id=case_id,
        timestamp=T0,
        completion=comp(model, error=error, cost=cost, latency=latency),
        claims=(),
        verdicts=(),
        scores=scores,
        seed=7,
        git_sha="x",
        prompt_hash="p",
    )


def run(records, models=("sonnet", "sol"), judges=(J1, J2), **kw: Any) -> RunSummary:
    pending = kw.pop("pending", False)
    base: dict[str, Any] = dict(
        run_id="r",
        task="pubmedqa",
        config_hash="h",
        models=models,
        seed=7,
        family_of=FAM.__getitem__,
        selection=JudgeSelection(
            judges=judges,
            judge_mode="dual" if len(judges) == 2 else "single",
            n_available=len(judges),
        ),
        judging=[
            JudgingResult(judge=j, status="pending" if pending and i == 0 else "complete")
            for i, j in enumerate(judges)
        ],
        started_at=T0,
        finished_at=None if pending else T1,
        n_resamples=500,
    )
    base.update(kw)
    return aggregate(records, **base)


def fixture(n=10):
    """sonnet: 8/10 correct, 6 of those grounded by J1 and 7 by J2, one transport error.
    sol: 5/10 correct, all grounded by both judges."""
    rs = []
    for i in range(n):
        cid = f"c{i:02d}"
        if i == 9:
            rs.append(rec("sonnet", cid, correct=False, error="transport"))
        else:
            rs.append(rec("sonnet", cid, correct=i < 8, grounded=(i < 6, i < 7)))
        rs.append(rec("sol", cid, correct=i < 5))
    return rs


# ---- AGG-01 headline ------------------------------------------------------------------------


def test_headline_is_correct_and_fully_grounded_over_all_cases_per_judge():
    s = run(fixture())
    sonnet = next(m for m in s.models if m.model == "sonnet")
    assert sonnet.headline[J1].point == pytest.approx(0.6)
    assert sonnet.headline[J2].point == pytest.approx(0.7)
    assert sonnet.accuracy.point == pytest.approx(0.8)
    sol = next(m for m in s.models if m.model == "sol")
    assert sol.headline[J1].point == sol.headline[J2].point == pytest.approx(0.5)


# ---- AGG-02 intervals -----------------------------------------------------------------------


def test_every_rate_has_an_interval_and_the_seed_makes_output_identical():
    a, b = run(fixture()), run(fixture())
    assert summary_json(a) == summary_json(b)
    m = a.models[0]
    for iv in (m.accuracy, m.citation_validity, m.coverage, *m.headline.values()):
        assert iv.lo <= iv.point <= iv.hi
    assert m.accuracy.hi > m.accuracy.lo  # 8/10 is not a degenerate sample
    assert summary_json(run(fixture(), seed=8)) != summary_json(a)


# ---- AGG-03 paired comparison and Holm ---------------------------------------------------------


def test_identical_models_tie_and_a_large_gap_separates():
    rs = []
    for i in range(200):
        cid = f"c{i:03d}"
        rs.append(rec("sonnet", cid, correct=i < 180))
        rs.append(rec("haiku", cid, correct=i < 180))
        rs.append(rec("sol", cid, correct=i < 60))
    s = run(rs, models=("sonnet", "haiku", "sol"))
    acc = {(c.model_a, c.model_b): c for c in s.comparisons if c.metric == "accuracy"}
    assert not acc["sonnet", "haiku"].separated and acc["sonnet", "haiku"].p_value == 1.0
    assert acc["sonnet", "sol"].separated and acc["sonnet", "sol"].diff.lo > 0
    heads = [c for c in s.comparisons if c.metric == "headline"]
    assert {c.judge for c in heads} == {J1, J2} and len(heads) == 6


def test_records_must_cover_the_same_cases_for_every_model():
    rs = fixture()[:-1]  # sol lost its last case
    with pytest.raises(ValueError, match="same cases"):
        run(rs)


# ---- AGG-04 thresholds ------------------------------------------------------------------------


def test_thresholds_all_must_hold_and_failures_are_named():
    th = (
        Threshold(metric="headline", op=">=", value=0.65),  # sonnet worst judge 0.6
        Threshold(metric="cost_per_correct_usd", op="<=", value=0.05),
    )
    s = run(fixture(), thresholds=th)
    sonnet = next(m for m in s.models if m.model == "sonnet")
    assert not sonnet.passes_threshold
    assert sonnet.failed_thresholds == ("headline >= 0.65 (observed 0.6)",)
    sol = next(m for m in s.models if m.model == "sol")
    assert sol.failed_thresholds == ("headline >= 0.65 (observed 0.5)",)
    ok = run(fixture(), thresholds=(Threshold(metric="accuracy", op=">=", value=0.5),))
    assert all(m.passes_threshold for m in ok.models)


def test_unknown_threshold_metric_is_rejected_when_config_loads():
    with pytest.raises(ValidationError):
        Threshold(metric="vibes", op=">=", value=1)  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        RunConfig.model_validate(
            {
                "models": ["a"],
                "judges": ["b"],
                "extractor": "c",
                "thresholds": [{"metric": "accuracyy", "op": ">=", "value": 0.5}],
            }
        )


# ---- AGG-05 error accounting -----------------------------------------------------------------


def test_errors_stay_in_the_denominator_and_are_counted_by_type():
    s = run(fixture())
    sonnet = next(m for m in s.models if m.model == "sonnet")
    assert sonnet.n == 10 and dict(sonnet.n_errors) == {"transport": 1}
    # dropping the errored case would give 8/9 accuracy; the summary must say 8/10
    assert sonnet.accuracy.point == pytest.approx(0.8)
    assert sonnet.cost_per_100_usd == pytest.approx(1.0)  # errored calls still cost
    assert sonnet.cost_per_correct_usd == pytest.approx(0.1 / 8)


def test_score_record_makes_failures_ungrounded_for_every_judge():
    case = Case(
        id="c", question="q", context=("Readmissions fell.",), reference_label="yes", source="t"
    )
    claim = Claim(id="c:m:0", text="Readmissions fell.", cited=(1,), extractor_version="e")
    cs = ClaimSet(claims=(claim,), addresses_question=(True,))
    v = {
        j: {
            claim.id: ClaimVerdict(
                claim_id=claim.id,
                judge=j,
                verdict="supported",
                evidence=(1,),
                rationale="",
                judge_prompt_hash="h",
            )
        }
        for j in (J1, J2)
    }
    ok = score_record(case, comp("sonnet"), cs, v)
    assert ok.correct_and_grounded == {J1: True, J2: True} and ok.coverage == 1.0
    for bad_comp, bad_cs in (
        (comp("sonnet", error="transport"), cs),
        (comp("sonnet"), ClaimSet(error="schema")),
        (comp("sonnet"), None),
    ):
        s = score_record(case, bad_comp, bad_cs, v)
        assert s.groundedness == {J1: 0.0, J2: 0.0} and not any(s.correct_and_grounded.values())
    partial = score_record(case, comp("sonnet"), cs, {J1: v[J1], J2: {}})
    assert partial.fully_grounded == {J1: True, J2: False}  # an unjudged claim is not grounded


# ---- AGG-06 self-preference ------------------------------------------------------------------


def test_self_preference_table_by_judge_and_family_only_in_dual_mode():
    rs = []
    for i in range(20):
        cid = f"c{i:02d}"
        rs.append(rec("sonnet", cid, correct=True, grounded=(True, i < 10)))
        rs.append(rec("sol", cid, correct=True, grounded=(i < 10, True)))
    s = run(rs)
    assert s.self_preference[J1]["anthropic"].point == pytest.approx(1.0)
    assert s.self_preference[J1]["openai"].point == pytest.approx(0.5)
    assert s.self_preference[J2]["openai"].point == pytest.approx(1.0)
    single_rs = [
        rec(r.completion.model, r.case_id, correct=True, grounded=(True,), judges=(J1,)) for r in rs
    ]
    single = run(single_rs, judges=(J1,))
    assert single.self_preference == {} and "single judge" in stamp_lines(single)[0]


# ---- AGG-07 summary.json and pending ---------------------------------------------------------


def test_pending_summary_has_no_per_judge_figures_and_writes_atomically(tmp_path):
    s = run(fixture(), pending=True)
    assert s.finished_at is None and all(not m.headline for m in s.models)
    assert all(c.metric == "accuracy" for c in s.comparisons) and s.self_preference == {}
    assert "judging pending" in " ".join(stamp_lines(s))
    out = tmp_path / "run" / "summary.json"
    write_summary(s, out)
    assert RunSummary.model_validate_json(out.read_text()) == s
    assert [p.name for p in out.parent.iterdir()] == ["summary.json"]  # no temp files left
    with pytest.raises(ValidationError, match="per-judge"):
        RunSummary.model_validate(
            {**s.model_dump(), "self_preference": {J1: {"anthropic": s.models[0].accuracy}}}
        )


def test_headline_threshold_while_pending_fails_with_a_reason():
    s = run(
        fixture(),
        pending=True,
        thresholds=(Threshold(metric="headline", op=">=", value=0.1),),
    )
    assert s.models[0].failed_thresholds == ("headline >= 0.1 (judging pending)",)


# ---- AGG-08 stamps ---------------------------------------------------------------------------


def test_stamps_are_derived_from_their_sources():
    s = run(fixture())
    assert s.judge_mode == "dual" and s.calibration_status == "uncalibrated"
    assert s.judging_status == "complete" and s.finished_at == T1
    assert stamp_lines(s) == [
        "judge not calibrated for this task (see docs/calibrate-your-task.md)"
    ]
    one_missing = run(fixture(), judging=[JudgingResult(judge=J1)])
    assert one_missing.judging_status == "pending" and one_missing.finished_at is None


def test_calibrated_only_with_agreement_for_every_judge_on_this_task_and_rubric():
    from evalkit.pipeline.judge import JUDGE_PROMPT_HASH

    iv = Interval(point=0.8, lo=0.7, hi=0.9)

    def agree(j, task="pubmedqa", h=JUDGE_PROMPT_HASH):
        return JudgeAgreement(
            judge=j,
            task=task,
            judge_prompt_hash=h,
            kappa=iv,
            agreement=0.9,
            confusion=((40, 5), (5, 50)),
            by_family={},
        )

    half = run(fixture(), agreements=[agree(J1)])
    assert half.calibration_status == "uncalibrated" and half.judges == ()
    full = run(fixture(), agreements=[agree(J1), agree(J2)])
    assert full.calibration_status == "calibrated" and {a.judge for a in full.judges} == {J1, J2}
    assert stamp_lines(full) == []
    stale = run(fixture(), agreements=[agree(J1), agree(J2, h="0" * 16)])
    assert stale.calibration_status == "uncalibrated"  # rubric changed since held-out scoring
    other = run(fixture(), agreements=[agree(J1, task="medqa"), agree(J2, task="medqa")])
    assert other.calibration_status == "uncalibrated" and other.judges == ()


def test_family_comes_from_the_provider():
    from evalkit.pipeline.aggregate import family_for

    assert family_for("claude-sonnet-5-5") == "anthropic"
    assert family_for("gpt-6-luna") == "openai"
    assert family_for("my-llama", {"my-llama": "http://localhost:8000/v1"}) == "other"


# ---- A2.5 verifier regressions (L-25) ---------------------------------------------------------


def test_stale_records_missing_a_finished_judge_are_refused():
    stale = [
        rec(r.completion.model, r.case_id, correct=True, grounded=(True,), judges=(J1,))
        for r in fixture()
    ]
    with pytest.raises(ValueError, match="no scores for judge"):
        run(stale)  # both judges complete, records only scored by J1


def test_a_model_whose_calls_all_failed_fails_every_threshold():
    rs = []
    for i in range(10):
        rs.append(rec("sonnet", f"c{i}", correct=False, error="transport", cost=0.0, latency=0))
        rs.append(rec("sol", f"c{i}", correct=True))
    th = (
        Threshold(metric="latency_p95_ms", op="<=", value=5000),
        Threshold(metric="cost_per_100_usd", op="<=", value=1),
    )
    s = run(rs, thresholds=th)
    sonnet = next(m for m in s.models if m.model == "sonnet")
    assert sonnet.latency_p50_ms is None and sonnet.latency_p95_ms is None
    assert not sonnet.passes_threshold and all(
        "all calls failed" in f for f in sonnet.failed_thresholds
    )
    assert next(m for m in s.models if m.model == "sol").passes_threshold


def test_latency_counts_schema_failures_but_not_transport():
    rs = [
        rec("sonnet", "c0", correct=False, error="schema", latency=4000),
        rec("sonnet", "c1", correct=False, error="transport", latency=0),
        rec("sonnet", "c2", correct=True, latency=1000),
    ]
    rs += [rec("sol", c, correct=True) for c in ("c0", "c1", "c2")]
    sonnet = next(m for m in run(rs).models if m.model == "sonnet")
    assert (sonnet.latency_p50_ms, sonnet.latency_p95_ms) == (1000, 4000)


def test_holm_turns_an_unadjusted_separation_into_a_tie():
    """Three models: one pair's p sits between 0.05/3 and 0.05, so unadjusted it would be
    "separated" but Holm (family of 3, smallest p needs <= 0.0167) calls it a tie."""
    from evalkit.stats import Resamples, paired

    n = 200
    rs_ = Resamples(n, 500, seed=7)  # the same resamples aggregate draws below

    def design(up, down):
        # a beats b on `up` cases, b beats a on `down` cases, the rest tie
        a = [1.0] * up + [0.0] * down + [1.0] * 60 + [0.0] * (n - up - down - 60)
        b = [0.0] * up + [1.0] * down + [1.0] * 60 + [0.0] * (n - up - down - 60)
        return a, b

    found = next(
        (
            (u, d)
            for u in range(4, 40)
            for d in range(0, 20)
            if 0.0167 < paired(*design(u, d), rs_)[1] < 0.05
        ),
        None,
    )
    assert found is not None
    a, b = design(*found)
    recs = []
    for i in range(n):
        cid = f"c{i:03d}"
        recs.append(rec("sonnet", cid, correct=a[i] == 1.0))
        recs.append(rec("sol", cid, correct=b[i] == 1.0))
        recs.append(rec("haiku", cid, correct=b[i] == 1.0))
    s = run(recs, models=("sonnet", "sol", "haiku"))
    acc = {(c.model_a, c.model_b): c for c in s.comparisons if c.metric == "accuracy"}
    pair = acc["sonnet", "sol"]
    assert 0.0167 < pair.p_value < 0.05 and not pair.separated
    assert not acc["sol", "haiku"].separated and acc["sol", "haiku"].p_value == 1.0


def test_misaligned_claim_sets_are_refused():
    from evalkit.pipeline.aggregate import build_records
    from evalkit.pipeline.generate import Generated

    case = Case(id="c1", question="q", context=("s",), reference_label="yes", source="t")
    claim = Claim(id="c1:sol:0", text="s", cited=(1,), extractor_version="e")
    gen = [Generated(case=case, model="sonnet", completion=comp("sonnet"))]
    with pytest.raises(ValueError, match="misaligned"):
        build_records(
            gen,
            [ClaimSet(claims=(claim,), addresses_question=(True,))],
            [],
            run_id="r",
            task="pubmedqa",
            seed=7,
            git_sha="x",
            prompt_hash="p",
            timestamp=T0,
        )


def test_inputs_from_another_run_or_duplicate_judging_are_refused():
    with pytest.raises(ValueError, match="not this run"):
        run(fixture(), run_id="other")
    with pytest.raises(ValueError, match="one judging result per judge"):
        run(
            fixture(),
            judging=[JudgingResult(judge=J1), JudgingResult(judge=J1), JudgingResult(judge=J2)],
        )
