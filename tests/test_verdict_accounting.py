"""Regressions reported by gemma-grounded-health (2026-11-17/18), end to end on the scripted
12-case fixture: (1) a verdict dropped from a batch must never let a run report judging
complete, and is counted; (2) the cost cap reserves only uncached work, and a batch refused
only because another judge's hold is still open waits for that hold to settle at its real
cost instead of stopping a run whose true cost fits."""

from __future__ import annotations

import asyncio
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evalkit import run as R
from evalkit.pipeline import judge as jm
from evalkit.pipeline.aggregate import aggregate, build_records, verdict_gaps
from evalkit.pipeline.extract import ClaimSet
from evalkit.pipeline.generate import Generated
from evalkit.pipeline.judge import JudgeSelection, JudgingResult
from evalkit.runtime import BudgetExceeded, Executor, Store
from evalkit.schemas import Answer, Case, Claim, ClaimVerdict, Completion
from tests.pipeline.fakes import ScriptedBatchAdapter
from tests.test_citation_support import (  # noqa: F401 - `project` is a fixture
    JUDGES,
    PRICES,
    T0,
    full_reply,
    project,
)

J1, J2 = JUDGES


def execute(task: str, adapter, *, max_cost: float | None = None, wait_s: float = 5.0):
    plan = R.plan_run(
        Path("evalkit.yaml"), task=task, n=12, out=Path("runs"), store_dir=Path("store")
    )
    outcome = asyncio.run(
        R.execute(
            plan,
            {"anthropic": adapter, "openai": adapter},
            PRICES,
            available_providers={"anthropic", "openai"},
            max_cost_usd=max_cost,
            wait_s=wait_s,
            now=lambda: T0,
        )
    )
    return plan, outcome


# ---- 1. a dropped verdict keeps judging pending, and is counted --------------------------------


@pytest.mark.usefixtures("project")
def test_a_verdict_dropped_from_a_batch_keeps_the_run_pending_until_resent():
    clean_plan, clean = execute("loc.yaml", ScriptedBatchAdapter(full_reply))
    assert clean.summary.judging_status == "complete"
    clean_bytes = (clean_plan.run_dir / "summary.json").read_bytes()
    assert b"missing_verdicts" not in clean_bytes
    shutil.rmtree("runs")
    shutil.rmtree("store")

    # The provider errors one claim-judge item (an "errored" batch line, as gemma's main and
    # parity runs saw): before the fix the claim was resubmittable but the run said complete.
    dropped: list[str] = []

    def one_error(cid: str) -> str | None:
        if not dropped and cid.startswith("j"):
            dropped.append(cid)
            return "errored:overloaded_error"
        return None

    fake = ScriptedBatchAdapter(full_reply, item_error=one_error)
    plan, outcome = execute("loc.yaml", fake)
    s = outcome.summary
    assert len(dropped) == 1
    assert outcome.exit_code == R.EXIT_PENDING == 4
    assert s.judging_status == "pending" and s.finished_at is None
    assert R.read_manifest(plan.run_dir).finished_at is None
    gaps = [(m.model, j, k) for m in s.models for j, k in m.missing_verdicts.items()]
    assert len(gaps) == 1 and gaps[0][2] == 1  # one model, one judge, one claim
    assert all(not m.headline and not m.groundedness_mean for m in s.models)

    # Rerunning the same command resends only that claim, and the run is then the clean run.
    fake.item_error = lambda cid: None
    batches = len(fake.submitted)
    plan, outcome = execute("loc.yaml", fake)
    assert outcome.summary.judging_status == "complete"
    assert len(fake.submitted) == batches + 1 and len(fake.submitted[-1]) == 1
    assert (plan.run_dir / "summary.json").read_bytes() == clean_bytes


CASE = Case(id="c1", question="q", context=("s1.", "s2."), reference_label="yes", source="t")


def _verdict(claim_id: str, judge: str) -> ClaimVerdict:
    return ClaimVerdict(
        claim_id=claim_id,
        judge=judge,
        verdict="supported",
        evidence=(1,),
        rationale="r",
        judge_prompt_hash=jm.JUDGE_PROMPT_HASH,
    )


def _records(judging: list[JudgingResult]):
    claims = tuple(
        Claim(id=f"c1:m:{i}", text=f"t{i}", cited=(1,), extractor_version="x") for i in range(3)
    )
    completion = Completion(
        model="m",
        model_snapshot="m-snap",
        params={"effort": "medium"},
        raw_text="{}",
        answer=Answer(label="yes", answer="a", citations=(1,)),
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
        cost_usd=0.01,
    )
    return build_records(
        [Generated(case=CASE, model="m", completion=completion)],
        [ClaimSet(claims=claims, addresses_question=(True,) * len(claims))],
        judging,
        run_id="r",
        task="pubmedqa",
        seed=7,
        git_sha="x",
        prompt_hash="p",
        timestamp=T0,
    )


def _summary(records, judging):
    return aggregate(
        records,
        run_id="r",
        task="pubmedqa",
        config_hash="h",
        models=("m",),
        seed=7,
        family_of=lambda m: "other",
        selection=JudgeSelection(judges=(J1, J2), judge_mode="dual", n_available=2),
        judging=judging,
        started_at=T0,
        finished_at=datetime(2026, 11, 17, 10, tzinfo=UTC),
        n_resamples=200,
    )


def test_complete_needs_every_claim_ruled_on_by_every_judge():
    ids = [f"c1:m:{i}" for i in range(3)]
    full = {i: _verdict(i, J1) for i in ids}
    short = {i: _verdict(i, J2) for i in ids[:2]}  # J2 never ruled on c1:m:2

    # Results that claim "complete" (as before the fix) cannot finish the run.
    judging = [JudgingResult(judge=J1, verdicts=full), JudgingResult(judge=J2, verdicts=short)]
    records = _records(judging)
    assert verdict_gaps(records, (J1, J2), judging) == ({"m": {J2: 1}}, True)
    s = _summary(records, judging)
    assert s.judging_status == "pending" and s.finished_at is None
    assert s.models[0].missing_verdicts == {J2: 1} and not s.models[0].headline

    # A final judge failure (refused, or invalid after the one retry) is an outcome: the run
    # completes, the claim is counted and scored unsupported, never dropped (H5, NFR5).
    judging[1].failed = {ids[2]: "schema"}
    records = _records(judging)
    assert records[0].judge_errors == {J2: {ids[2]: "schema"}}
    assert records[0].scores is not None
    assert records[0].scores.groundedness == {J1: 1.0, J2: pytest.approx(2 / 3)}
    assert records[0].scores.fully_grounded == {J1: True, J2: False}
    s = _summary(records, judging)
    assert s.judging_status == "complete"
    assert s.models[0].missing_verdicts == {J2: 1}
    assert s.models[0].groundedness_mean[J2].point == pytest.approx(2 / 3)
    assert "missing_verdicts" in s.model_dump_json()

    # Every claim ruled on: nothing is reported, so existing summaries keep their bytes.
    judging[1] = JudgingResult(judge=J2, verdicts={i: _verdict(i, J2) for i in ids})
    s = _summary(_records(judging), judging)
    assert s.judging_status == "complete" and "missing_verdicts" not in s.model_dump_json()


# ---- 2. the cap reserves uncached work only, and waits for open holds --------------------------


def _citation_holds(plan) -> tuple[dict[str, float], dict[str, int]]:
    """What each judge's citation batch reserves, and how many calls it makes."""
    records, cases = R.read_records(plan.run_dir), R.read_cases(plan.run_dir)
    holds = dict.fromkeys(JUDGES, 0.0)
    calls = dict.fromkeys(JUDGES, 0)
    for r in records:
        for c in r.claims:
            for j in JUDGES:
                if jm.CITATIONS.pre(c, cases[r.case_id], j) is None:
                    inp = jm.CITATIONS.make_input(c, cases[r.case_id], j, plan.config.judge_effort)
                    req = jm.CITATIONS.request(inp)
                    holds[j] += jm._estimate(PRICES[j], req, batch=True)
                    calls[j] += 1
    return holds, calls


@pytest.mark.usefixtures("project")
def test_a_cache_heavy_rescore_whose_true_cost_fits_the_cap_completes():
    fake = ScriptedBatchAdapter(full_reply)
    plain, _ = execute("loc.yaml", fake)  # answers, claims and claim verdicts cached
    holds, calls = _citation_holds(plain)
    actual = {j: 0.001 * n for j, n in calls.items()}  # what the scripted adapter bills
    # gemma's rescore: both citation batches at once do not fit, but the first one's real
    # cost plus the second one's hold does. Cached work holds nothing.
    cap = max(actual[J1] + holds[J2], actual[J2] + holds[J1]) * 1.001
    assert cap < holds[J1] + holds[J2]
    before = len(fake.requests)
    plan, outcome = execute("loc_cite.yaml", fake, max_cost=cap)
    assert outcome.summary.judging_status == "complete" and outcome.exit_code in (0, 1)
    new = fake.requests[before:]
    assert {r.model for r in new} == {"citation_judge"} and len(new) == sum(calls.values())
    assert outcome.spent_usd == pytest.approx(sum(actual.values()))
    assert not R.read_manifest(plan.run_dir).stopped_at_cost_cap


@pytest.mark.usefixtures("project")
def test_a_rescore_whose_uncached_work_exceeds_the_cap_still_stops():
    fake = ScriptedBatchAdapter(full_reply)
    plain, _ = execute("loc.yaml", fake)
    holds, calls = _citation_holds(plain)
    # Room for the cheaper judge's batch only: the uncached work (true cost and holds alike)
    # exceeds the cap, so the run stops however the two judges are ordered.
    cap = min(holds.values()) * 1.001
    assert 0.001 * sum(calls.values()) > cap and max(holds.values()) > cap
    batches = len(fake.submitted)
    with pytest.raises(BudgetExceeded):
        execute("loc_cite.yaml", fake, max_cost=cap)
    assert len(fake.submitted) == batches + 1  # one citation batch sent, the other stopped
    plan = R.plan_run(
        Path("evalkit.yaml"), task="loc_cite.yaml", n=12, out=Path("runs"), store_dir=Path("store")
    )
    manifest = R.read_manifest(plan.run_dir)
    assert manifest.stopped_at_cost_cap and manifest.spent_usd <= cap


def test_cached_claims_hold_nothing_and_a_judge_waits_for_another_judges_hold(tmp_path):
    from tests.pipeline.test_judge import CASE as JCASE
    from tests.pipeline.test_judge import CFG, claim, supported

    items = [(JCASE, claim(i, (1,), f"Readmissions fell {i}.")) for i in range(12)]
    fake = ScriptedBatchAdapter(supported)
    ads = {"anthropic": fake, "openai": fake}

    async def both(ex: Executor, todo):
        return await asyncio.gather(
            *(
                jm.judge_claims(
                    ex, todo, j, CFG, ads, PRICES, run_id="r", max_wait_s=5, poll_interval_s=0.01
                )
                for j in JUDGES
            )
        )

    asyncio.run(both(Executor(Store(tmp_path)), items[:10]))

    def hold(j: str, todo=items[10:]) -> float:
        return sum(
            jm._estimate(
                PRICES[j], jm._request(jm.judge_input(c, JCASE, j, CFG.judge_effort)), batch=True
            )
            for _, c in todo
        )

    # Two fresh claims per judge: both holds at once exceed the cap, the true cost fits.
    cap = max(0.002 + hold(J2), 0.002 + hold(J1)) * 1.001
    assert cap < hold(J1) + hold(J2)
    ex = Executor(Store(tmp_path), max_cost_usd=cap)
    sent = len(fake.submitted)
    results = asyncio.run(both(ex, items))
    assert all(r.status == "complete" and len(r.verdicts) == 12 for r in results)
    assert [len(b) for b in fake.submitted[sent:]] == [2, 2]  # only the uncached claims
    assert ex.spent_usd == pytest.approx(0.004) and ex.in_flight_usd == pytest.approx(0.0)

    # Uncached work that cannot fit even after the other hold settles still stops.
    fresh = [(JCASE, claim(i, (1,), f"Costs rose {i}.")) for i in range(12, 14)]
    cap = min(hold(J1, fresh), hold(J2, fresh)) * 1.001
    assert cap < 0.004 and max(hold(J1, fresh), hold(J2, fresh)) > cap
    tight = Executor(Store(tmp_path), max_cost_usd=cap)
    sent = len(fake.submitted)
    with pytest.raises(BudgetExceeded):
        asyncio.run(both(tight, [*items, *fresh]))
    assert len(fake.submitted) == sent + 1
