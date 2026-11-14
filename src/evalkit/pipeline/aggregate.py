"""Aggregate (AGG-01..08): scored records -> `summary.json`.

Every rate is a mean over all cases a model was asked, never only successful calls (NFR5),
with a percentile bootstrap 95% interval from one seeded resample matrix shared by every
model and metric (D-37). Pairwise gaps are "separated" only when they survive Holm across the
model pairs of that metric and judge (OWNER D-36).
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from itertools import combinations
from pathlib import Path
from typing import Literal

from evalkit.pipeline.extract import ClaimSet
from evalkit.pipeline.extract import coverage as coverage_score
from evalkit.pipeline.generate import Generated, provider_for
from evalkit.pipeline.judge import (
    JUDGE_PROMPT_HASH,
    JudgeSelection,
    JudgingResult,
    groundedness,
)
from evalkit.schemas import Case, ClaimVerdict, Completion, Interval, Record, RunSummary
from evalkit.schemas._base import SCHEMA_VERSION
from evalkit.schemas.benchmark import Scores
from evalkit.schemas.config import Threshold
from evalkit.schemas.summary import JudgeAgreement, ModelSummary, PairComparison
from evalkit.scorers.deterministic import citation_valid, label_correct
from evalkit.stats import Resamples, holm, interval, paired, percentile_ms

Family = Literal["anthropic", "openai", "other"]
PER_JUDGE_METRICS = ("headline", "groundedness")


def family_for(model: str, endpoints: Mapping[str, str] | None = None) -> Family:
    """Generator family for the self-preference table, from the model's provider."""
    provider = provider_for(model, endpoints)
    return provider if provider in ("anthropic", "openai") else "other"  # type: ignore[return-value]


def score_record(
    case: Case,
    completion: Completion,
    claim_set: ClaimSet | None,
    verdicts: Mapping[str, Mapping[str, ClaimVerdict]],
) -> Scores:
    """Scores for one answer. `verdicts` maps judge -> claim id -> verdict for the judges that
    finished; a judge still pending is simply absent. A failed call, a missing answer or a
    failed extraction is ungrounded for every judge (AGG-05); a claim a judge never ruled on
    keeps the answer from being fully grounded (JDG-06)."""
    correct = label_correct(case, completion).value == 1.0
    usable = (
        completion.error is None
        and completion.answer is not None
        and claim_set is not None
        and claim_set.error is None
    )
    ground: dict[str, float] = {}
    fully: dict[str, bool] = {}
    for judge, by_claim in verdicts.items():
        if usable and claim_set is not None:
            g, f = groundedness(claim_set.claims, by_claim)
        else:
            g, f = 0.0, False
        ground[judge], fully[judge] = g, f
    return Scores(
        label_correct=correct,
        citation_valid=citation_valid(case, completion).value,
        groundedness=ground,
        fully_grounded=fully,
        coverage=coverage_score(claim_set).value if usable and claim_set is not None else 0.0,
        correct_and_grounded={j: correct and f for j, f in fully.items()},
    )


def build_records(
    generated: Sequence[Generated],
    claim_sets: Sequence[ClaimSet],
    judging: Sequence[JudgingResult],
    *,
    run_id: str,
    task: str,
    seed: int,
    git_sha: str,
    prompt_hash: str,
    timestamp: datetime,
) -> list[Record]:
    """One scored Record per generated answer. Only judges whose results are complete
    contribute verdicts and per-judge scores (D-37)."""
    if len(generated) != len(claim_sets):
        raise ValueError("one claim set per generated answer")
    done = {r.judge: r.verdicts for r in judging if r.status == "complete"}
    out = []
    for g, cs in zip(generated, claim_sets, strict=True):
        prefix = f"{g.case.id}:{g.model}:"
        if any(not c.id.startswith(prefix) for c in cs.claims):
            raise ValueError(
                f"claim set does not belong to {g.case.id}/{g.model}: lists misaligned"
            )
        per_judge = {j: {c.id: vs[c.id] for c in cs.claims if c.id in vs} for j, vs in done.items()}
        out.append(
            Record(
                schema_version=SCHEMA_VERSION,
                run_id=run_id,
                task=task,
                case_id=g.case.id,
                timestamp=timestamp,
                completion=g.completion,
                claims=cs.claims,
                verdicts=tuple(v for j in sorted(per_judge) for v in per_judge[j].values()),
                scores=score_record(g.case, g.completion, cs, per_judge),
                seed=seed,
                git_sha=git_sha,
                prompt_hash=prompt_hash,
            )
        )
    return out


def _observed(m: ModelSummary, metric: str) -> float | None:
    """Value a threshold is checked against; per-judge metrics use the worst judge."""
    if metric == "headline":
        return min((iv.point for iv in m.headline.values()), default=None)
    if metric == "groundedness":
        return min((iv.point for iv in m.groundedness_mean.values()), default=None)
    simple: dict[str, float | None] = {
        "accuracy": m.accuracy.point,
        "citation_validity": m.citation_validity.point,
        "coverage": m.coverage.point,
        "cost_per_100_usd": m.cost_per_100_usd,
        "cost_per_correct_usd": m.cost_per_correct_usd,
        "latency_p50_ms": None if m.latency_p50_ms is None else float(m.latency_p50_ms),
        "latency_p95_ms": None if m.latency_p95_ms is None else float(m.latency_p95_ms),
    }
    return simple[metric]


def aggregate(
    records: Sequence[Record],
    *,
    run_id: str,
    task: str,
    config_hash: str,
    models: Sequence[str],
    thresholds: Sequence[Threshold] = (),
    seed: int,
    family_of: Callable[[str], Family],
    selection: JudgeSelection,
    judging: Sequence[JudgingResult],
    agreements: Sequence[JudgeAgreement] = (),
    started_at: datetime,
    finished_at: datetime | None = None,
    n_resamples: int = 10_000,
) -> RunSummary:
    """Build the run summary. The stamps are derived, never passed in (AGG-08):
    `judge_mode` from the judge selection, `judging_status` from the judging results, and
    `calibration_status` from whether held-out agreement exists for every judge (A2.6)."""
    judges = selection.judges
    judge_mode = selection.judge_mode
    judging_status = judging_status_of(judges, judging)
    calibration_status = calibration_status_of(judges, agreements, task=task)
    by_model: dict[str, dict[str, Record]] = {m: {} for m in models}
    for r in records:
        if r.run_id != run_id or r.task != task:
            raise ValueError(f"record {r.case_id} belongs to {r.run_id}/{r.task}, not this run")
        if r.scores is None:
            raise ValueError(f"record {r.case_id}/{r.completion.model} has not been scored")
        if r.completion.model not in by_model:
            raise ValueError(f"record for unknown model {r.completion.model!r}")
        if r.case_id in by_model[r.completion.model]:
            raise ValueError(f"duplicate record {r.case_id}/{r.completion.model}")
        by_model[r.completion.model][r.case_id] = r
    case_ids = sorted(next(iter(by_model.values())))
    for m, rows in by_model.items():
        if sorted(rows) != case_ids:
            raise ValueError(f"{m} did not answer the same cases; paired comparison needs them")
    if not case_ids:
        raise ValueError("no records to aggregate")
    complete = judging_status == "complete"
    if complete:
        for r in records:
            assert r.scores is not None
            stale = [
                j
                for j in judges
                if j not in r.scores.correct_and_grounded or j not in r.scores.groundedness
            ]
            if stale:
                raise ValueError(
                    f"record {r.case_id}/{r.completion.model} has no scores for judge(s) {stale}: "
                    "rebuild records after judging completes"
                )
    rs = Resamples(len(case_ids), n_resamples, seed=seed)

    def vec(m: str, f: Callable[[Record], float]) -> list[float]:
        return [f(by_model[m][c]) for c in case_ids]

    def sc(r: Record) -> Scores:
        assert r.scores is not None
        return r.scores

    acc = {m: vec(m, lambda r: float(sc(r).label_correct)) for m in models}
    # Per-judge vectors exist only once every judge has finished (D-37): pending records
    # carry no per-judge scores at all.
    pairs = [(m, j) for m in models for j in judges] if complete else []
    head = {(m, j): vec(m, lambda r, j=j: float(sc(r).correct_and_grounded[j])) for m, j in pairs}
    ground = {(m, j): vec(m, lambda r, j=j: sc(r).groundedness[j]) for m, j in pairs}

    summaries: list[ModelSummary] = []
    for m in models:
        rows = [by_model[m][c] for c in case_ids]
        n = len(rows)
        # fsum: exact, so totals are identical on every Python (3.12 changed float sum()).
        total_cost = math.fsum(r.completion.cost_usd for r in rows)
        n_correct = sum(acc[m])
        # Latency of calls that reached the provider: schema failures and refusals took real
        # time; transport failures have no meaningful latency.
        ok_latency = [r.completion.latency_ms for r in rows if r.completion.error != "transport"]
        errors = Counter(r.completion.error for r in rows if r.completion.error is not None)
        summary = ModelSummary(
            model=m,
            family=family_of(m),
            n=n,
            n_errors=dict(errors),
            headline={j: interval(head[m, j], rs) for j in judges} if complete else {},
            accuracy=interval(acc[m], rs),
            groundedness_mean={j: interval(ground[m, j], rs) for j in judges} if complete else {},
            citation_validity=interval(vec(m, lambda r: sc(r).citation_valid), rs),
            coverage=interval(vec(m, lambda r: sc(r).coverage), rs),
            cost_per_100_usd=total_cost / n * 100,
            cost_per_correct_usd=total_cost / n_correct if n_correct else None,
            latency_p50_ms=percentile_ms(ok_latency, 0.50) if ok_latency else None,
            latency_p95_ms=percentile_ms(ok_latency, 0.95) if ok_latency else None,
            passes_threshold=True,
            failed_thresholds=(),
        )
        failed = []
        all_failed = sum(errors.values()) == n
        for t in thresholds:
            metric, op, value = t.metric, t.op, t.value
            seen = _observed(summary, metric)
            if all_failed:  # no working call can pass any bar, cost and latency included
                failed.append(f"{metric} {op} {value} (all calls failed)")
            elif seen is None:
                why = "judging pending" if metric in PER_JUDGE_METRICS else "undefined"
                failed.append(f"{metric} {op} {value} ({why})")
            elif not t.holds(seen):
                failed.append(f"{metric} {op} {value} (observed {seen:.4g})")
        summaries.append(
            summary.model_copy(
                update={"passes_threshold": not failed, "failed_thresholds": tuple(failed)}
            )
        )

    comparisons: list[PairComparison] = []
    families: list[
        tuple[Literal["accuracy", "headline"], str | None, Mapping[str, list[float]]]
    ] = [("accuracy", None, acc)]
    if complete:
        for j in judges:
            families.append(("headline", j, {m: head[m, j] for m in models}))
    for metric, judge, values in families:
        tested = {(a, b): paired(values[a], values[b], rs) for a, b in combinations(models, 2)}
        separated = holm({k: p for k, (_, p) in tested.items()})
        comparisons.extend(
            PairComparison(
                metric=metric,
                judge=judge,
                model_a=a,
                model_b=b,
                diff=iv,
                p_value=p,
                separated=separated[a, b],
            )
            for (a, b), (iv, p) in tested.items()
        )

    self_pref: dict[str, dict[str, Interval]] = {}
    if complete and judge_mode == "dual":
        for j in judges:
            self_pref[j] = {}
            for fam in sorted({family_of(m) for m in models}):
                members = [m for m in models if family_of(m) == fam]
                per_case = [
                    math.fsum(ground[m, j][i] for m in members) / len(members)
                    for i in range(len(case_ids))
                ]
                self_pref[j][fam] = interval(per_case, rs)

    return RunSummary(
        schema_version=SCHEMA_VERSION,
        run_id=run_id,
        task=task,
        config_hash=config_hash,
        judge_mode=judge_mode,
        calibration_status=calibration_status,
        models=tuple(summaries),
        judges=tuple(
            a
            for a in agreements
            if a.judge in judges
            and a.task == task
            and a.judge_prompt_hash == JUDGE_PROMPT_HASH
            and a.split == "heldout"
        )
        if calibration_status == "calibrated"
        else (),
        self_preference=self_pref,
        comparisons=tuple(comparisons),
        judging_status=judging_status,
        started_at=started_at,
        finished_at=finished_at if complete else None,
    )


def judging_status_of(
    judges: Sequence[str], judging: Sequence[JudgingResult]
) -> Literal["pending", "complete"]:
    """Complete only when every selected judge has a result and none is pending."""
    names = [r.judge for r in judging]
    if len(names) != len(set(names)):
        raise ValueError("one judging result per judge")
    by_judge = {r.judge: r for r in judging}
    missing = [j for j in judges if j not in by_judge]
    if missing or any(by_judge[j].status == "pending" for j in judges):
        return "pending"
    return "complete"


def calibration_status_of(
    judges: Sequence[str],
    agreements: Sequence[JudgeAgreement],
    *,
    task: str,
    rubric_hash: str = JUDGE_PROMPT_HASH,
) -> Literal["calibrated", "uncalibrated"]:
    """Calibrated only with held-out agreement for every selected judge, scored on this task
    under the current rubric (Tech Spec §5, CAL-05)."""
    have = {
        a.judge
        for a in agreements
        if a.task == task and a.judge_prompt_hash == rubric_hash and a.split == "heldout"
    }
    return "calibrated" if judges and all(j in have for j in judges) else "uncalibrated"


def stamp_lines(summary: RunSummary) -> list[str]:
    """Caveats every chart, report and CLI summary must show with the numbers (AGG-08)."""
    lines = []
    if summary.judge_mode == "single":
        lines.append("single judge: self-preference not measured")
    if summary.calibration_status == "uncalibrated":
        lines.append("judge not calibrated for this task (see docs/calibrate-your-task.md)")
    if summary.judging_status == "pending":
        lines.append("judging pending: headline and groundedness not yet available")
    return lines


def summary_json(summary: RunSummary) -> str:
    return json.dumps(summary.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"


def write_summary(summary: RunSummary, path: Path) -> None:
    """Atomic write: readers see the old file or the new one, never a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".summary-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(summary_json(summary))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
