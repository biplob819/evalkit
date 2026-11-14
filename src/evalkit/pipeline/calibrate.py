"""Calibration (CAL-01..08): how far each judge can be trusted on this task, against the
owner's hand labels (Tech Spec §5).

Flow: `export_sample` picks 100 answers stratified by model and reference label, split 50
tuning / 50 held-out, plus 20 for a blind relabel. `build_sheet` turns them into blind rows
(opaque keys, redacted text, cited sentences only). The owner labels them in the local page
(`evalkit.pipeline.labeller`, OWNER D-40) or the CSV. `import_labels` validates the labels,
and `calibration_report` scores the held-out split per judge, stamped with the rubric hash.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import math
import random
import secrets
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import Field

from evalkit.pipeline.judge import JUDGE_PROMPT_HASH, redact
from evalkit.schemas import (
    Case,
    ClaimVerdict,
    Interval,
    Record,
    RunSummary,
)
from evalkit.schemas._base import SCHEMA_VERSION, FrozenMap, Model, NonBlank
from evalkit.schemas.calibration import CalibrationLabel, CalibrationReport
from evalkit.schemas.summary import JudgeAgreement
from evalkit.stats import Resamples, _quantile

Split = Literal["tuning", "heldout"]
KAPPA_DROP_TOLERANCE = 0.05  # CAL-06: a larger held-out kappa drop fails CI


MIN_JUDGE_COVERAGE = 0.95  # a judge must rule on this share of eligible held-out claims


def _opaque(prefix: str, secret: str, text: str, n: int = 12) -> str:
    """Keyed digest: opaque to the labeller and not reversible by guessing claim ids."""
    return prefix + hmac.new(secret.encode(), text.encode(), hashlib.sha256).hexdigest()[:n]


# ---- CAL-01 sample ---------------------------------------------------------------------------


class SampledAnswer(Model):
    record_key: NonBlank  # f"{case_id}:{model}" (private: never rendered)
    case_id: NonBlank
    model: NonBlank
    split: Split
    relabel: bool


def _allocate(sizes: Mapping[tuple[str, str], int], n: int) -> dict[tuple[str, str], int]:
    """Largest-remainder proportional allocation, capped by stratum size."""
    total = sum(sizes.values())
    if n >= total:
        return dict(sizes)
    exact = {k: n * s / total for k, s in sizes.items()}
    out = {k: min(sizes[k], math.floor(v)) for k, v in exact.items()}
    order = sorted(sizes, key=lambda k: (-(exact[k] - math.floor(exact[k])), k))
    i = 0
    while sum(out.values()) < n:
        k = order[i % len(order)]
        if out[k] < sizes[k]:
            out[k] += 1
        i += 1
    return out


def export_sample(
    records: Sequence[Record],
    cases: Mapping[str, Case],
    *,
    seed: int,
    n: int = 100,
    n_relabel: int = 20,
) -> list[SampledAnswer]:
    """Stratified by (model, reference label); only answers with claims can be labelled.
    Within the sample, answers alternate tuning / held-out in stratum order, so each split
    is itself stratified, and the relabel set takes equally from both splits."""
    if n % 2 or n_relabel % 2 or n_relabel > n:
        raise ValueError("n and n_relabel must be even, with n_relabel <= n")
    keys = [f"{r.case_id}:{r.completion.model}" for r in records]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate records for one case and model")
    strata: dict[tuple[str, str], list[Record]] = defaultdict(list)
    for r in records:
        if not r.claims:
            continue
        label = cases[r.case_id].reference_label or "\x00unlabelled"  # never a real label
        strata[(r.completion.model, label)].append(r)
    if sum(len(v) for v in strata.values()) < n:
        raise ValueError(
            f"only {sum(len(v) for v in strata.values())} labellable answers; need {n}"
        )
    rng = random.Random(f"evalkit-calibration:{seed}")
    for k in sorted(strata):
        strata[k].sort(key=lambda r: (r.case_id, r.completion.model))
        rng.shuffle(strata[k])
    take = _allocate({k: len(v) for k, v in strata.items()}, n)
    chosen = [r for k in sorted(strata) for r in strata[k][: take[k]]]
    splits: list[Split] = ["tuning" if i % 2 == 0 else "heldout" for i in range(len(chosen))]
    picks = {"tuning": [], "heldout": []}  # type: dict[str, list[int]]
    for i, s in enumerate(splits):
        picks[s].append(i)
    relabel: set[int] = set()
    for s in ("tuning", "heldout"):
        relabel.update(rng.sample(picks[s], n_relabel // 2))
    return [
        SampledAnswer(
            record_key=f"{r.case_id}:{r.completion.model}",
            case_id=r.case_id,
            model=r.completion.model,
            split=splits[i],
            relabel=i in relabel,
        )
        for i, r in enumerate(chosen)
    ]


# ---- CAL-02 blind sheet and labels -----------------------------------------------------------


class SheetRow(Model):
    sheet_id: NonBlank  # which export these rows (and any labels for them) belong to
    key: NonBlank  # opaque claim key shown to the labeller
    answer: NonBlank  # opaque answer key shown to the labeller
    split: Split
    relabel: bool
    claim_id: NonBlank  # private: joins labels to verdicts; never rendered
    claim_text: str  # redacted
    cited: tuple[tuple[int, str], ...]  # (sentence number, redacted text)


def build_sheet(
    sample: Sequence[SampledAnswer],
    records: Sequence[Record],
    cases: Mapping[str, Case],
    *,
    seed: int,
    secret: str | None = None,
) -> list[SheetRow]:
    """Blind rows for labelling. Answers appear in a seeded shuffle (claims of one answer stay
    together), so the order does not reveal which model wrote them. Keys are keyed digests of
    the claim id *and* its text and citations, so labels can never attach to different claims
    from another run or extractor version."""
    secret = secret or secrets.token_hex(16)
    sheet_id = _opaque("s", secret, "sheet")
    by_key = {f"{r.case_id}:{r.completion.model}": r for r in records}
    order = list(sample)
    random.Random(f"evalkit-sheet:{seed}").shuffle(order)
    rows = []
    for s in order:
        rec = by_key[s.record_key]
        case = cases[s.case_id]
        for c in rec.claims:
            cited = tuple(
                (n, redact(case.context[n - 1])) for n in c.cited if 1 <= n <= len(case.context)
            )
            text = redact(c.text)
            rows.append(
                SheetRow(
                    sheet_id=sheet_id,
                    key=_opaque("k", secret, json.dumps([c.id, text, cited])),
                    answer=_opaque("a", secret, s.record_key, 10),
                    split=s.split,
                    relabel=s.relabel,
                    claim_id=c.id,
                    claim_text=text,
                    cited=cited,
                )
            )
    return rows


CSV_COLUMNS = ("key", "answer", "split", "claim", "cited_sentences", "label", "extraction_error")


def sheet_csv(rows: Sequence[SheetRow]) -> str:
    """The blind CSV: opaque keys, claim and cited sentences, empty label columns."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(CSV_COLUMNS)
    for r in rows:
        cited = " | ".join(f"[{n}] {t}" for n, t in r.cited)
        w.writerow([r.key, r.answer, r.split, r.claim_text, cited, "", ""])
    return buf.getvalue()


class LabelEntry(Model):
    label: Literal["supported", "unsupported"]
    extraction_error: bool = False


class LabelFile(Model):
    """One round of labels for one sheet. The round and sheet are part of the file, so a
    relabel can never read or overwrite first-pass labels, and labels made for one export can
    never be imported against another."""

    sheet_id: NonBlank
    round: Literal["first", "relabel"]
    labels: FrozenMap[LabelEntry] = Field(default_factory=dict)


def labels_from_csv(text: str, rows: Sequence[SheetRow]) -> LabelFile:
    """Read a filled CSV (first round). Unknown or duplicate keys, edited claim text and
    invalid labels are errors; blank labels are skipped (still to do)."""
    if not rows:
        raise ValueError("empty sheet")
    by_key = {r.key: r for r in rows}
    out: dict[str, LabelEntry] = {}
    for i, row in enumerate(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))), start=2):
        key = row.get("key", "")
        if key not in by_key:
            raise ValueError(f"line {i}: unknown claim key {key!r} (labels from another sheet?)")
        if key in out:
            raise ValueError(f"line {i}: claim key {key!r} appears twice")
        if row.get("claim", "") != by_key[key].claim_text:
            raise ValueError(f"line {i}: claim text was edited; labels must refer to the original")
        raw = (row.get("label") or "").strip().casefold()
        if not raw:
            continue
        label = {"s": "supported", "u": "unsupported"}.get(raw, raw)
        if label not in ("supported", "unsupported"):
            raise ValueError(f"line {i}: label must be supported/unsupported (or S/U), got {raw!r}")
        flag = (row.get("extraction_error") or "").strip().casefold()
        err = flag in ("1", "y", "yes", "true", "x")
        out[key] = LabelEntry(label=label, extraction_error=err)  # type: ignore[arg-type]
    return LabelFile(sheet_id=rows[0].sheet_id, round="first", labels=out)


def import_labels(
    rows: Sequence[SheetRow],
    first: LabelFile,
    relabel: LabelFile | None = None,
    *,
    require_complete: bool = True,
) -> list[CalibrationLabel]:
    """Validated labels: first-pass labels carry their split; relabel-round labels carry
    split="relabel". Files must belong to this sheet and the right round."""
    if not rows:
        raise ValueError("empty sheet")
    sheet_id = rows[0].sheet_id
    for f, want in ((first, "first"), (relabel, "relabel")):
        if f is None:
            continue
        if f.sheet_id != sheet_id:
            raise ValueError(f"{want}-round labels belong to another sheet ({f.sheet_id})")
        if f.round != want:
            raise ValueError(f"expected {want}-round labels, got {f.round}-round labels")
    by_key = {r.key: r for r in rows}
    for k in [*first.labels, *(relabel.labels if relabel else {})]:
        if k not in by_key:
            raise ValueError(f"label for unknown claim key {k!r}")
    if require_complete:
        missing = [r.key for r in rows if r.key not in first.labels]
        if missing:
            raise ValueError(f"{len(missing)} claim(s) have no first-pass label, e.g. {missing[0]}")
    out = [
        CalibrationLabel(
            claim_id=by_key[k].claim_id,
            split=by_key[k].split,
            label=e.label,
            extraction_error=e.extraction_error,
        )
        for k, e in first.labels.items()
    ]
    for k, e in (relabel.labels if relabel else {}).items():
        if not by_key[k].relabel:
            raise ValueError(f"claim {k} is not in the relabel set")
        out.append(
            CalibrationLabel(
                claim_id=by_key[k].claim_id,
                split="relabel",
                label=e.label,
                extraction_error=e.extraction_error,
            )
        )
    return out


# ---- CAL-03/04 agreement ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Pair:
    answer: str
    human: bool  # supported?
    judge: bool


def _confusion(pairs: Sequence[_Pair]) -> tuple[int, int, int, int]:
    tp = sum(p.human and p.judge for p in pairs)
    fn = sum(p.human and not p.judge for p in pairs)
    fp = sum(not p.human and p.judge for p in pairs)
    tn = sum(not p.human and not p.judge for p in pairs)
    return tp, fn, fp, tn


def kappa_from(tp: int, fn: int, fp: int, tn: int) -> float:
    """Cohen's kappa for two raters on a binary label. When chance agreement is total (both
    raters constant) kappa is undefined; it is reported as 1.0 if they agree, else 0.0."""
    n = tp + fn + fp + tn
    if n == 0:
        return 0.0
    po = (tp + tn) / n
    pe = ((tp + fn) * (tp + fp) + (fp + tn) * (fn + tn)) / (n * n)
    if pe >= 1.0:
        return 1.0 if po == 1.0 else 0.0
    return (po - pe) / (1 - pe)


def cluster_kappa(
    pairs: Sequence[_Pair], *, seed: int, n_resamples: int = 10_000, alpha: float = 0.05
) -> Interval:
    """Kappa with a percentile bootstrap that resamples answers, not claims: claims in one
    answer share a context and a writer, so treating them as independent would overstate
    precision."""
    if not pairs:
        raise ValueError("no labelled claims to compare")
    answers = sorted({p.answer for p in pairs})
    per: dict[str, list[int]] = {a: [0, 0, 0, 0] for a in answers}
    for p in pairs:
        c = per[p.answer]
        i = (0 if p.judge else 1) if p.human else (2 if p.judge else 3)
        c[i] += 1
    point = kappa_from(*_confusion(pairs))
    if len(answers) < 2:
        return Interval(point=point, lo=point, hi=point)
    rs = Resamples(len(answers), max(n_resamples, 100), seed=seed)
    counts = [per[a] for a in answers]
    boot = []
    for row in rs.rows:
        tp = fn = fp = tn = 0
        for idx in row:
            c = counts[idx]
            tp += c[0]
            fn += c[1]
            fp += c[2]
            tn += c[3]
        boot.append(kappa_from(tp, fn, fp, tn))
    boot.sort()
    lo, hi = _quantile(boot, alpha / 2), _quantile(boot, 1 - alpha / 2)
    return Interval(point=point, lo=min(lo, point), hi=max(hi, point))


def _answer_of(claim_id: str) -> str:
    return claim_id.rsplit(":", 1)[0]  # "{case}:{model}:{i}" -> "{case}:{model}"


class InsufficientCoverage(ValueError):
    pass


def agreement(
    labels: Sequence[CalibrationLabel],
    verdicts: Mapping[str, ClaimVerdict],
    *,
    judge: str,
    task: str,
    family_of: Callable[[str], str],
    seed: int,
    split: Literal["heldout", "tuning"] = "heldout",
    rubric_hash: str | None = None,
    n_resamples: int = 10_000,
    min_coverage: float = MIN_JUDGE_COVERAGE,
) -> JudgeAgreement:
    """Agreement with the owner on one split, first-pass labels only (held-out certifies;
    tuning is for rubric work and is marked as such). Claims flagged as extraction errors are
    left out; the judge must have ruled on at least `min_coverage` of the rest."""
    rubric_hash = rubric_hash or JUDGE_PROMPT_HASH
    eligible = [lab for lab in labels if lab.split == split and not lab.extraction_error]
    if not eligible:
        raise ValueError(f"no labelled {split} claims")
    for lab in eligible:
        v = verdicts.get(lab.claim_id)
        if v is not None and v.judge_prompt_hash != rubric_hash:
            raise ValueError("verdicts were made under a different rubric than the one certified")
    covered = [lab for lab in eligible if lab.claim_id in verdicts]
    coverage = len(covered) / len(eligible)
    if coverage < min_coverage:
        raise InsufficientCoverage(
            f"{judge} ruled on {len(covered)} of {len(eligible)} {split} claims "
            f"({coverage:.0%}); at least {min_coverage:.0%} is needed to certify"
        )
    pairs = [
        _Pair(
            _answer_of(lab.claim_id),
            lab.label == "supported",
            verdicts[lab.claim_id].verdict == "supported",
        )
        for lab in covered
    ]
    tp, fn, fp, tn = _confusion(pairs)
    by_family: dict[str, Interval] = {}
    for fam in sorted({family_of(p.answer.split(":", 1)[1]) for p in pairs}):
        sub_pairs = [p for p in pairs if family_of(p.answer.split(":", 1)[1]) == fam]
        by_family[fam] = cluster_kappa(sub_pairs, seed=seed, n_resamples=n_resamples)
    return JudgeAgreement(
        judge=judge,
        task=task,
        judge_prompt_hash=rubric_hash,
        split=split,
        n_claims=len(pairs),
        coverage=coverage,
        kappa=cluster_kappa(pairs, seed=seed, n_resamples=n_resamples),
        agreement=(tp + tn) / len(pairs),
        confusion=((tp, fn), (fp, tn)),
        by_family=by_family,
    )


def extraction_error_rate(labels: Sequence[CalibrationLabel]) -> float:
    first = [lab for lab in labels if lab.split != "relabel"]
    return sum(lab.extraction_error for lab in first) / len(first) if first else 0.0


def self_consistency(
    labels: Sequence[CalibrationLabel], *, seed: int, n_resamples: int = 10_000
) -> Interval | None:
    """Kappa between the owner's relabel and first pass, on claims labelled in both rounds."""
    first = {lab.claim_id: lab for lab in labels if lab.split != "relabel"}
    pairs = [
        _Pair(
            _answer_of(lab.claim_id),
            first[lab.claim_id].label == "supported",
            lab.label == "supported",
        )
        for lab in labels
        if lab.split == "relabel" and lab.claim_id in first
    ]
    return cluster_kappa(pairs, seed=seed, n_resamples=n_resamples) if pairs else None


class HeldoutReused(ValueError):
    pass


def heldout_digest(labels: Sequence[CalibrationLabel]) -> str:
    ids = sorted(lab.claim_id for lab in labels if lab.split == "heldout")
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()


def calibration_report(
    labels: Sequence[CalibrationLabel],
    verdicts_by_judge: Mapping[str, Mapping[str, ClaimVerdict]],
    *,
    task: str,
    family_of: Callable[[str], str],
    seed: int,
    scored_at: datetime,
    previous: Sequence[CalibrationReport] = (),
    n_resamples: int = 10_000,
) -> CalibrationReport:
    """Score the held-out split once for the current rubric (CAL-05). A held-out set already
    scored under a different rubric is refused: after a rubric change, label a fresh one."""
    heldout = [lab for lab in labels if lab.split == "heldout"]
    digest = heldout_digest(labels)
    for old in previous:
        if old.heldout_digest == digest and old.judge_prompt_hash != JUDGE_PROMPT_HASH:
            raise HeldoutReused(
                "this held-out set was already scored under another rubric "
                f"({old.judge_prompt_hash}); label a fresh held-out set (Tech Spec §5.7)"
            )
    return CalibrationReport(
        schema_version=SCHEMA_VERSION,
        task=task,
        judge_prompt_hash=JUDGE_PROMPT_HASH,
        scored_at=scored_at,
        n_answers=len({_answer_of(lab.claim_id) for lab in heldout}),
        n_claims=len(heldout),
        heldout_digest=digest,
        agreements=tuple(
            agreement(
                labels,
                verdicts,
                judge=j,
                task=task,
                family_of=family_of,
                seed=seed,
                n_resamples=n_resamples,
            )
            for j, verdicts in sorted(verdicts_by_judge.items())
        ),
        extraction_error_rate=extraction_error_rate(labels),
        self_consistency=self_consistency(labels, seed=seed, n_resamples=n_resamples),
    )


def usable_agreements(report: CalibrationReport | None, task: str) -> tuple[JudgeAgreement, ...]:
    """Agreements that still certify this task: none once the rubric has changed (CAL-05)."""
    if report is None or report.task != task or report.judge_prompt_hash != JUDGE_PROMPT_HASH:
        return ()
    return report.agreements


# ---- CAL-06 regression gate ------------------------------------------------------------------


def kappa_regressions(
    baseline: Mapping[str, float],
    current: Mapping[str, float],
    tolerance: float = KAPPA_DROP_TOLERANCE,
) -> list[str]:
    """Judges whose held-out kappa fell by more than `tolerance`, or vanished."""
    problems = []
    for judge, base in sorted(baseline.items()):
        now = current.get(judge)
        if now is None:
            problems.append(f"{judge}: no held-out kappa in the current run")
        elif round(base - now, 9) > tolerance:  # an exact 0.05 drop passes, consistently
            problems.append(f"{judge}: held-out kappa fell {base:.3f} -> {now:.3f}")
    return problems


def regression_problems(fixture: Mapping[str, object], current: Mapping[str, float]) -> list[str]:
    """CAL-06: a fixture recorded under another rubric must be re-recorded (re-judge the
    held-out claims, compare kappa to the baseline, then update) before CI can pass."""
    problems = []
    if fixture.get("judge_prompt_hash") != JUDGE_PROMPT_HASH:
        problems.append(
            f"judge rubric changed ({fixture.get('judge_prompt_hash')} -> {JUDGE_PROMPT_HASH}): "
            "re-judge the held-out claims and re-record the regression fixture"
        )
    baseline = fixture.get("baseline_kappa")
    if not isinstance(baseline, dict):
        return [*problems, "fixture has no baseline_kappa"]
    return problems + kappa_regressions(baseline, current)


# ---- CAL-07 publish guard --------------------------------------------------------------------


class PublishRefused(Exception):
    pass


def publish_guard(summary: RunSummary) -> None:
    """B8: a published headline needs both judges and calibration for this task and rubric."""
    missing = []
    if summary.judge_mode != "dual":
        missing.append("both judges (this run is single-judge)")
    if summary.calibration_status != "calibrated":
        missing.append("held-out calibration for this task and rubric")
    if summary.judging_status != "complete":
        missing.append("finished judging")
    if missing:
        raise PublishRefused("cannot publish: needs " + "; ".join(missing))
