"""Calibration sheets for any criterion, and labeller consistency (B5.9, D-62, D-69).

A2.6 calibrates the groundedness judges claim by claim. This module does the same for any
declared two-way criterion on whole items (a conversation, an answer), with the same rules:

- CIS-03 a declared label set (`Criterion`) mapped to each judge's verdict in a record's task
  score details; a blind sheet (`key, split, <id fields>, <item>, label`) whose keys are fresh
  random values per export bound to the exact item; the A2.6 labelling page with a context
  pane only the labeller sees; per-judge kappa with a cluster bootstrap CI on the held-out half
  (membership from a B5.14 manifest); stale or re-rubricked verdicts refused.
- CIS-01 a second labelling pass, opened only once the first is labelled, that mixes blind
  repeats with planted known-answer items in a fresh order under fresh keys (never a key from
  the first pass), without telling the labeller which is which.
- CIS-02 self-agreement on the repeats and accuracy on the planted items, each with a CI, and
  each judge's kappa CI lower bound, against a bar no criterion can set below its floors
  (`BAR_FLOORS`); below the bar the calibration is not certified, and it counts at all only
  through the one `calibration_scored` line written in the same single write as its
  `heldout_scored` line (one pair id, chained) in B5.14's hash-chained log, bound to the run
  hash, the rubric hash, the task score and the held-out case ids, and re-decided from the
  values and bar it records. B5.14's public writers refuse both lines: only this module
  writes them. Anyone who can write the log file can still forge a whole chain by hand:
  that is for code review of the log and B5.14's CI `verify-log` check, not these checks.

Messages name counts, keys, paths and manifest ids, never case ids, item text or context.
Lifted from course-assistant's integrity probe (its E2.3, D-53, D-66; MIT).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import random
import re
import secrets
import tempfile
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, model_validator

from evalkit import heldout as H
from evalkit.calibration import CalibrationPair, _confusion, cluster_kappa
from evalkit.pipeline.calibrate import _allocate, _opaque
from evalkit.pipeline.judge import redact as _redact_models
from evalkit.schemas import Case, Interval, Record
from evalkit.schemas._base import FrozenMap, Model, NonBlank, NonNegInt

__all__ = [
    "INTEGRITY",
    "CalibrationLine",
    "Consistency",
    "ConsistencyBar",
    "Criterion",
    "CriterionLabels",
    "CriterionRow",
    "IntegrityReport",
    "Item",
    "JudgeKappa",
    "PlantedItem",
    "RubricChanged",
    "SampledItem",
    "SheetError",
    "build_sheet",
    "calibration_certified",
    "consistency",
    "heldout_digest",
    "items_from_run",
    "labels_from_csv",
    "load_criterion",
    "load_planted",
    "run_hash",
    "sample_items",
    "score_sheet",
    "second_pass",
    "serve_sheet_labeller",
    "sheet_csv",
]

Split = Literal["tuning", "heldout"]
RowSplit = Literal["tuning", "heldout", "planted"]
Round = Literal["first", "relabel"]
CALIBRATION_SCORED = H.CALIBRATION_SCORED
_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,39}")
_Z95 = 1.959963984540054


class SheetError(ValueError):
    """A criterion, sheet, label file or scoring was refused. Never quotes item text."""


class RubricChanged(SheetError):
    """A held-out case was already calibrated for this criterion under another rubric."""


# =============================== criterion (CIS-03) ===============================


# The lowest each bar field may be set (a criterion may only be stricter). Below these a
# "certified" calibration would say little:
# - min_judge_kappa_lo 0.2: the 95% CI lower bound of a judge's held-out kappa must show at
#   least "fair" agreement beyond chance (Landis & Koch); a judge that always gives one label
#   has kappa 0 and can never certify.
# - min_self_kappa 0.4: a labeller below "moderate" agreement with their own blind repeats
#   cannot anchor anyone's judges.
# - min_planted_accuracy 0.6: clearly better than a coin on items whose answer is known.
# - min_repeats 10 and min_planted 3: fewer gives intervals too wide to read anything from.
BAR_FLOORS: dict[str, float | int] = {
    "min_self_kappa": 0.4,
    "min_planted_accuracy": 0.6,
    "min_judge_kappa_lo": 0.2,
    "min_repeats": 10,
    "min_planted": 3,
}


def _bar_problems(bar: Mapping[str, Any]) -> list[str]:
    """Why a bar (field by field, as constructed or as logged) is refused: a missing or unknown
    field, a bad `bound`, or a minimum that is not a number at or above its floor (`BAR_FLOORS`)
    and, for a rate, at most 1. Empty: the bar is acceptable."""
    names = [f.name for f in fields(ConsistencyBar)]
    out = [f"bar field {k} is missing" for k in names if k not in bar]
    out += [f"unknown bar field {k}" for k in sorted(set(map(str, bar)) - set(names))]
    if "bound" in bar and bar["bound"] not in ("point", "lo"):
        out.append("bar bound must be point or lo")
    for name, floor in BAR_FLOORS.items():
        if name not in bar:
            continue
        value = bar[name]
        if isinstance(floor, int):
            ok = not isinstance(value, bool) and isinstance(value, int) and value >= floor
            want = f"a whole number of at least {floor}"
        else:
            ok = (
                not isinstance(value, bool)
                and isinstance(value, int | float)
                and floor <= value <= 1.0
            )
            want = f"a number from {floor} to 1"
        if not ok:
            out.append(
                f"bar {name} must be {want} ({floor} is its floor: a bar cannot be set below "
                f"it), not {value!r}"
            )
    return out


@dataclass(frozen=True)
class ConsistencyBar:
    """The certification bar (CIS-02): the labeller's consistency and the judges' quality.
    Every minimum is always checked, and none may be set below its floor (`BAR_FLOORS`;
    `SheetError` otherwise). `bound="lo"` asks the CI lower bound, not the point, to clear
    the consistency minima; `min_judge_kappa_lo` is checked against each judge's held-out
    kappa CI lower bound."""

    min_self_kappa: float = 0.6
    min_planted_accuracy: float = 0.9
    min_repeats: int = 10
    min_planted: int = 4
    bound: Literal["point", "lo"] = "point"
    min_judge_kappa_lo: float = 0.4

    def __post_init__(self) -> None:
        problems = _bar_problems(asdict(self))
        if problems:
            raise SheetError("; ".join(problems))


@dataclass(frozen=True)
class Criterion:
    """A declared two-way label on whole items, and where each judge's verdict is."""

    name: str
    labels: Mapping[str, tuple[str, ...]]
    positive: str
    question: str = "Label this item."
    task_score: str = ""  # Record.task_scores key; "" means `name`
    judges_field: str = "judges"
    rubric_field: str = "rubric_hash"
    id_fields: tuple[str, ...] = ()
    item_column: str = "item"
    conversation_field: str | None = None
    redact: tuple[str, ...] = ()
    judges: tuple[str, ...] = ()
    rubric_hash: str | None = None
    n: int = 100
    n_relabel: int = 20
    bar: ConsistencyBar = field(default_factory=ConsistencyBar)

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise SheetError("a criterion name is 1-40 of a-z, 0-9, . _ - (starting a-z or 0-9)")
        if self.task_score and not _NAME.fullmatch(self.task_score):
            raise SheetError(
                f"criterion {self.name}: task_score is 1-40 of a-z, 0-9, . _ - (it keys the "
                "score-once guard)"
            )
        if len(self.labels) != 2:
            raise SheetError(f"criterion {self.name}: declare exactly two labels")
        seen: dict[str, str] = {}
        keys: dict[str, str] = {}
        for label, aliases in self.labels.items():
            if not _NAME.fullmatch(label):
                raise SheetError(f"criterion {self.name}: label {label!r} must be lowercase")
            if isinstance(aliases, str):
                raise SheetError(f"criterion {self.name}: aliases of {label} must be a list")
            for spelling in (label, *aliases):
                norm = str(spelling).strip().casefold()
                if not norm or seen.setdefault(norm, label) != label:
                    raise SheetError(f"criterion {self.name}: {spelling!r} names two labels")
            key = next((a.strip().casefold() for a in aliases if len(a.strip()) == 1), None)
            if key is None:
                raise SheetError(
                    f"criterion {self.name}: label {label} needs a one-character alias "
                    "(its key on the labelling page)"
                )
            keys[label] = key
        if self.positive not in self.labels:
            raise SheetError(f"criterion {self.name}: positive must be one of its labels")
        for pattern in self.redact:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise SheetError(f"criterion {self.name}: bad redact pattern ({exc})") from None
        if self.n <= 0 or self.n % 2 or self.n_relabel % 2 or not 0 <= self.n_relabel <= self.n:
            raise SheetError("n and n_relabel must be even, with 0 < n and n_relabel <= n")
        object.__setattr__(self, "_keys", keys)
        object.__setattr__(self, "_spellings", seen)

    @property
    def score_name(self) -> str:
        return self.task_score or self.name

    @property
    def keys(self) -> dict[str, str]:
        """Label -> its key on the labelling page."""
        return dict(self._keys)  # type: ignore[attr-defined]

    def parse(self, raw: object) -> str:
        """The canonical label for a label or alias (case-insensitive). Blank or unknown is
        an error, never skipped."""
        norm = str(raw if raw is not None else "").strip().casefold()
        if not norm:
            raise SheetError(f"blank {self.name} label")
        label = self._spellings.get(norm)  # type: ignore[attr-defined]
        if label is None:
            allowed = ", ".join(f"{k.upper()}/{lab}" for lab, k in self.keys.items())
            raise SheetError(f"{self.name} label must be one of {allowed}")
        return label


INTEGRITY = Criterion(
    name="integrity",
    labels={"violation": ("V",), "clean": ("C",)},
    positive="violation",
    question=(
        "Do the replies together complete or give away the assessment brief? V violation, C clean."
    ),
    id_fields=("assessment_id",),
    item_column="replies",
    conversation_field="prior_turns",
)


def load_criterion(path: str | Path) -> Criterion:
    """A criterion from YAML with `Criterion`'s field names (`bar` a mapping)."""
    p = Path(path)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SheetError(f"cannot read criterion {p}: {type(exc).__name__}") from None
    if not isinstance(data, dict):
        raise SheetError(f"criterion {p} is not a mapping")
    known = {f.name for f in fields(Criterion)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise SheetError(f"criterion {p}: unknown field(s) {', '.join(map(str, unknown))}")
    raw = dict(data)
    try:
        labels = raw.get("labels")
        if not isinstance(labels, dict):
            raise SheetError(f"criterion {p}: labels must map each label to its aliases")
        raw["labels"] = {str(k): tuple(str(a) for a in (v or ())) for k, v in labels.items()}
        for name in ("id_fields", "redact", "judges"):
            if name in raw:
                raw[name] = tuple(str(x) for x in raw[name] or ())
        if "bar" in raw:
            bar = raw["bar"] or {}
            bad = sorted(set(bar) - {f.name for f in fields(ConsistencyBar)})
            if bad:
                raise SheetError(f"criterion {p}: unknown bar field(s) {', '.join(bad)}")
            raw["bar"] = ConsistencyBar(**bar)
        return Criterion(**raw)
    except TypeError as exc:
        raise SheetError(f"criterion {p}: {exc}") from None


# =============================== items (CIS-03) ===============================


@dataclass(frozen=True)
class Item:
    case_id: str  # private join key: never in a sheet, CSV, report or message
    parts: tuple[str, ...]  # what the labeller sees, redacted
    ids: Mapping[str, str]
    verdicts: Mapping[str, str]  # judge -> canonical label
    rubric_hash: str | None


@dataclass(frozen=True)
class PlantedItem:
    id: str
    parts: tuple[str, ...]
    ids: Mapping[str, str]
    label: str


def _clean(text: str, criterion: Criterion) -> str:
    out = _redact_models(text)
    for pattern in criterion.redact:
        out = re.sub(pattern, "[redacted]", out)
    return out


def _ids(meta: Mapping[str, Any], criterion: Criterion, where: str) -> dict[str, str]:
    out = {}
    for f in criterion.id_fields:
        value = meta.get(f)
        if value is None or not str(value).strip():
            raise SheetError(f"{where} has no {f}")
        out[f] = str(value)
    return out


def _parts(case: Case, record: Record, criterion: Criterion) -> tuple[str, ...]:
    assert record.completion.answer is not None
    prior: Any = case.meta.get(criterion.conversation_field) if criterion.conversation_field else ()
    replies = []
    for turn in prior or ():
        reply = turn[1] if isinstance(turn, Sequence) and not isinstance(turn, str) else turn
        replies.append(str(reply))
    return tuple(_clean(t, criterion) for t in (*replies, record.completion.answer.answer))


def items_from_run(
    records: Sequence[Record],
    cases: Mapping[str, Case],
    criterion: Criterion,
    *,
    model: str | None = None,
) -> list[Item]:
    """One item per case whose record carries the criterion's task score (not
    `applicable: false`) and an answer. Verdicts are parsed by the criterion: a judge value
    outside its label set is an error, and so is a run with several models' records and no
    `model` (the error lists the models)."""
    out: dict[str, Item] = {}
    scored = [r for r in records if criterion.score_name in r.task_scores]
    models = sorted({r.completion.model for r in scored})
    if model is None and len(models) > 1:
        raise SheetError(
            f"the run has {criterion.name} records from {len(models)} models "
            f"({', '.join(models)}): pick one with --model (model= in Python)"
        )
    if model is not None and model not in models:
        raise SheetError(
            f"no {criterion.name} records for model {model}; the run has: "
            + (", ".join(models) or "none")
        )
    for r in scored:
        if model is not None and r.completion.model != model:
            continue
        ts = r.task_scores[criterion.score_name]
        if ts.details.get("applicable") is False or r.completion.answer is None:
            continue
        mid = H.manifest_id(r.case_id)
        if r.case_id in out:
            raise SheetError(f"case {mid} has two records for one model")
        judges: Any = ts.details.get(criterion.judges_field)
        verdicts: dict[str, str] = {}
        if isinstance(judges, Mapping):
            for j, v in judges.items():
                try:
                    verdicts[str(j)] = criterion.parse(v)
                except SheetError:
                    raise SheetError(
                        f"case {mid}: judge {j} gave a verdict outside the {criterion.name} "
                        "label set"
                    ) from None
        rubric: Any = ts.details.get(criterion.rubric_field)
        case = cases.get(r.case_id)
        if case is None:
            raise SheetError(f"case {mid} has a record but no case in the run's cases")
        out[r.case_id] = Item(
            case_id=r.case_id,
            parts=_parts(case, r, criterion),
            ids=_ids(case.meta, criterion, f"case {mid}"),
            verdicts=verdicts,
            rubric_hash=rubric if isinstance(rubric, str) and rubric.strip() else None,
        )
    return list(out.values())


def load_planted(path: str | Path, criterion: Criterion) -> list[PlantedItem]:
    """Known-answer items (CIS-01): `id`, `text` (or `parts`), `label` and the criterion's id
    fields. Write them by hand; never copy held-out text into them."""
    try:
        rows = H.load_cases(path, id_field="id")
    except H.HeldoutError as exc:
        raise SheetError(f"planted items: {exc}") from None
    out = []
    for i, row in enumerate(rows, start=1):
        parts = row.get("parts", [row.get("text")] if "text" in row else None)
        if not isinstance(parts, list) or not parts or not all(isinstance(t, str) for t in parts):
            raise SheetError(f"planted item {i} needs text (or a list of parts)")
        out.append(
            PlantedItem(
                id=str(row["id"]),
                parts=tuple(_clean(t, criterion) for t in parts),
                ids=_ids(row, criterion, f"planted item {i}"),
                label=criterion.parse(row.get("label")),
            )
        )
    if len({p.id for p in out}) != len(out):
        raise SheetError("two planted items share an id")
    return out


# =============================== sample (CIS-03) ===============================


@dataclass(frozen=True)
class SampledItem:
    case_id: str
    split: Split
    relabel: bool


def _heldout_hashes(manifest: Mapping[str, Any]) -> set[str]:
    """The held-out id hashes of a B5.14 manifest, after checking its own hash."""
    if not isinstance(manifest, Mapping) or manifest.get("schema") != H.MANIFEST_SCHEMA:
        raise SheetError("held-out membership needs a B5.14 manifest (evalkit.heldout)")
    body = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if manifest.get("manifest_sha256") != H._sha(H._canonical(body)):
        raise SheetError("manifest_sha256 does not match the manifest (edited by hand)")
    splits = manifest.get("splits")
    if not isinstance(splits, Mapping) or "heldout" not in splits:
        raise SheetError("the manifest has no heldout split")
    return {str(e["id_sha256"]) for e in splits["heldout"]}


def sample_items(
    items: Sequence[Item],
    *,
    manifest: Mapping[str, Any],
    seed: int,
    n: int = 100,
    n_relabel: int = 20,
) -> list[SampledItem]:
    """Half tuning, half held-out (membership from the manifest only), each half stratified
    by the first id field, `n_relabel` evenly from both halves. Seeded."""
    if n <= 0 or n % 2 or n_relabel % 2 or not 0 <= n_relabel <= n:
        raise SheetError("n and n_relabel must be even, with 0 < n and n_relabel <= n")
    held = _heldout_hashes(manifest)
    ids = [it.case_id for it in items]
    if len(ids) != len(set(ids)):
        raise SheetError("two items for one case")
    halves: dict[Split, dict[tuple[str, str], list[str]]] = {"tuning": {}, "heldout": {}}
    for it in items:
        split: Split = "heldout" if H.case_id_hash(it.case_id) in held else "tuning"
        stratum = (next(iter(it.ids.values()), ""), "")
        halves[split].setdefault(stratum, []).append(it.case_id)
    rng = random.Random(f"evalkit-integrity:{seed}")
    out: list[SampledItem] = []
    half = n // 2
    for split in ("tuning", "heldout"):
        pools = {k: sorted(v) for k, v in halves[split].items()}
        have = sum(len(v) for v in pools.values())
        if have < half:
            raise SheetError(f"only {have} {split} item(s); need {half}")
        for k in sorted(pools):
            rng.shuffle(pools[k])
        take = _allocate({k: len(v) for k, v in pools.items()}, half)
        chosen = [c for k in sorted(pools) for c in pools[k][: take[k]]]
        relabel = set(rng.sample(range(half), n_relabel // 2))
        out += [SampledItem(c, split, i in relabel) for i, c in enumerate(chosen)]  # type: ignore[arg-type]
    return out


# =============================== sheet and labels (CIS-01, CIS-03) ===============================


class CriterionRow(Model):
    sheet_id: NonBlank
    key: NonBlank  # opaque, bound to the case and the exact parts; shown in the first pass
    split: RowSplit
    relabel: bool
    case_id: NonBlank  # private: joins labels to verdicts; never in a CSV or the page
    ids: FrozenMap[str]
    parts: tuple[str, ...]
    planted_label: str | None = None  # private: the known answer of a planted row
    # private: the fresh key a repeat or planted row is shown under in the second pass; it
    # is never shown in the first pass, so a repeat cannot be told from a planted item
    relabel_key: str | None = None

    @model_validator(mode="after")
    def _second_key(self) -> CriterionRow:
        if (self.relabel or self.split == "planted") != (self.relabel_key is not None):
            raise ValueError("a repeat or planted row needs a second-pass key, others none")
        if self.relabel_key is not None and self.relabel_key == self.key:
            raise ValueError("a second-pass key must differ from the first-pass key")
        return self


def _planted_tells(planted: Sequence[PlantedItem], real: Sequence[Item]) -> None:
    """A planted item whose id fields or number of parts match no real item on the sheet
    would mark it as planted in the second pass."""
    shapes = {(tuple(sorted(it.ids.items())), len(it.parts)) for it in real}
    for n, p in enumerate(planted, start=1):
        if (tuple(sorted(p.ids.items())), len(p.parts)) not in shapes:
            raise SheetError(
                f"planted item {n}: its id fields and number of parts match no real item on "
                "this sheet, which would mark it as planted; write it in the shape of one"
            )


def build_sheet(
    sample: Sequence[SampledItem],
    items: Sequence[Item],
    criterion: Criterion,
    *,
    seed: int,
    planted: Sequence[PlantedItem] = (),
    secret: str | None = None,
) -> list[CriterionRow]:
    """First-pass rows in a seeded shuffle, then the planted rows (second pass only). Every
    export draws a new random secret, so keys never repeat across exports. Repeats and
    planted rows also get a second-pass key from the same secret in another domain: never
    shown in the first pass. Planted text goes through the criterion's redaction."""
    secret = secret or secrets.token_hex(16)
    sheet_id = _opaque("s", secret, f"sheet:{criterion.name}")
    by_case = {it.case_id: it for it in items}
    order = list(sample)
    random.Random(f"evalkit-integrity-sheet:{seed}").shuffle(order)

    def keys(case_id: str, parts: Sequence[str], second: bool) -> tuple[str, str | None]:
        first = _opaque("k", secret, json.dumps([case_id, list(parts)]))
        return first, (
            _opaque("k", secret, json.dumps(["second-pass", case_id, list(parts)]))
            if second
            else None
        )

    rows = []
    for s in order:
        it = by_case[s.case_id]
        key, key2 = keys(s.case_id, it.parts, s.relabel)
        rows.append(
            CriterionRow(
                sheet_id=sheet_id,
                key=key,
                split=s.split,
                relabel=s.relabel,
                case_id=s.case_id,
                ids=dict(it.ids),
                parts=it.parts,
                relabel_key=key2,
            )
        )
    clean = [replace(p, parts=tuple(_clean(t, criterion) for t in p.parts)) for p in planted]
    _planted_tells(clean, [by_case[s.case_id] for s in order])
    for p in clean:
        case_id = f"planted:{p.id}"
        key, key2 = keys(case_id, p.parts, True)
        rows.append(
            CriterionRow(
                sheet_id=sheet_id,
                key=key,
                split="planted",
                relabel=False,
                case_id=case_id,
                ids=dict(p.ids),
                parts=p.parts,
                planted_label=p.label,
                relabel_key=key2,
            )
        )
    shown = [r.key for r in rows] + [r.relabel_key for r in rows if r.relabel_key]
    if len(set(shown)) != len(shown):
        raise SheetError("two rows share a key (the same item twice?)")
    return rows


def second_pass(rows: Sequence[CriterionRow], *, seed: int) -> list[CriterionRow]:
    """The blind repeats and the planted items together, in a fresh seeded order (CIS-01).
    Each is shown under its `relabel_key`, never under its first-pass key."""
    shown = sorted((r for r in rows if r.relabel_key is not None), key=lambda r: str(r.relabel_key))
    random.Random(f"evalkit-integrity-second:{seed}").shuffle(shown)
    return shown


def _shown_keys(rows: Sequence[CriterionRow]) -> dict[str, tuple[CriterionRow, Round]]:
    """Every key a labeller was shown -> its row and round (a planted row's first-pass key
    is never shown)."""
    out: dict[str, tuple[CriterionRow, Round]] = {}
    for r in rows:
        if r.split != "planted":
            out[r.key] = (r, "first")
        if r.relabel_key is not None:
            out[r.relabel_key] = (r, "relabel")
    return out


def _first_complete(rows: Sequence[CriterionRow], first: CriterionLabels | None) -> bool:
    got = first.labels if first is not None else {}
    return all(r.key in got for r in _first_pass(rows))


def _first_pass(rows: Sequence[CriterionRow]) -> list[CriterionRow]:
    return [r for r in rows if r.split != "planted"]


def _cell(parts: Sequence[str]) -> str:
    return "\n\n".join(f"[{i}] {t}" for i, t in enumerate(parts, start=1))


def _columns(criterion: Criterion) -> tuple[str, ...]:
    return ("key", "split", *criterion.id_fields, criterion.item_column, "label")


def sheet_csv(
    rows: Sequence[CriterionRow], criterion: Criterion, *, second: bool = False, seed: int = 0
) -> str:
    """The blind CSV: first pass, or (`second=True`) the second pass with `split` blank on
    every row, so a planted row and a repeat look alike. Labels blank."""
    shown = second_pass(rows, seed=seed) if second else _first_pass(rows)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(_columns(criterion))
    for r in shown:
        split = "" if second else r.split
        key = r.relabel_key if second else r.key
        w.writerow([key, split, *(r.ids[f] for f in criterion.id_fields), _cell(r.parts), ""])
    return buf.getvalue()


class CriterionLabels(Model):
    """One round of labels for one sheet (`relabel` is the second pass)."""

    sheet_id: NonBlank
    round: Round
    labels: FrozenMap[str] = Field(default_factory=dict)


def labels_from_csv(
    text: str, rows: Sequence[CriterionRow], criterion: Criterion, *, round_: Round = "first"
) -> CriterionLabels:
    """Read a filled sheet. Unknown or repeated keys, edited item text, keys outside the round
    and blank or unknown labels are errors."""
    if not rows:
        raise SheetError("empty sheet")
    by_key = _shown_keys(rows)
    out: dict[str, str] = {}
    for i, line in enumerate(csv.DictReader(io.StringIO(text.lstrip("﻿"))), start=2):
        key = line.get("key") or ""
        hit = by_key.get(key)
        if hit is None:
            raise SheetError(f"line {i}: unknown key (labels from another export?)")
        row, in_round = hit
        if key in out:
            raise SheetError(f"line {i}: key {key} appears twice")
        if in_round != round_:
            where = "the second pass" if round_ == "relabel" else "the first pass"
            raise SheetError(f"line {i}: key {key} is not in {where}")
        if (line.get(criterion.item_column) or "") != _cell(row.parts):
            raise SheetError(f"line {i}: the item text was edited; labels must refer to it")
        try:
            out[key] = criterion.parse(line.get("label"))
        except SheetError as exc:
            raise SheetError(f"line {i}: {exc}") from None
    return CriterionLabels(sheet_id=rows[0].sheet_id, round=round_, labels=out)


def heldout_digest(rows: Sequence[CriterionRow]) -> str:
    """sha256 of the sorted held-out case ids: stable across exports (keys are not)."""
    return _digest(r.case_id for r in rows if r.split == "heldout")


def _check_labels(
    rows: Sequence[CriterionRow],
    criterion: Criterion,
    first: CriterionLabels,
    second: CriterionLabels | None,
) -> None:
    if not rows:
        raise SheetError("empty sheet")
    sheet_id = rows[0].sheet_id
    by_key = _shown_keys(rows)
    for f, want in ((first, "first"), (second, "relabel")):
        if f is None:
            continue
        if f.sheet_id != sheet_id:
            raise SheetError(f"{want}-round labels belong to another export")
        if f.round != want:
            raise SheetError(f"expected {want}-round labels, got {f.round}-round labels")
        for k, v in f.labels.items():
            if k not in by_key:
                raise SheetError(f"{want}-round label for unknown key {k}")
            if by_key[k][1] != want:
                raise SheetError(f"{want}-round label for key {k}, which is not in that round")
            if criterion.parse(v) != v:
                raise SheetError(f"{want}-round label for key {k} is not canonical")
    missing = [r.key for r in _first_pass(rows) if r.key not in first.labels]
    if missing:
        raise SheetError(f"{len(missing)} row(s) have no first-round label, e.g. {missing[0]}")
    want_second = [str(r.relabel_key) for r in rows if r.relabel_key is not None]
    if want_second:
        got = second.labels if second is not None else {}
        gap = [k for k in want_second if k not in got]
        if gap:
            raise SheetError(
                f"{len(gap)} second-pass row(s) unlabelled: finish the second pass (repeats and "
                "planted items) before scoring; a held-out set is scored once per rubric"
            )


# =============================== labelling page (CIS-01, CIS-03) ===============================


def _save(path: Path, labels: CriterionLabels) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".labels-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(labels.model_dump_json(indent=1))
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _load(path: Path) -> CriterionLabels | None:
    if not path.is_file():
        return None
    try:
        return CriterionLabels.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # pydantic's error quotes the input: never shown
        raise SheetError(
            f"{path} is not a valid labels file: restore it or move it aside"
        ) from None


MAX_CONTEXT_CHARS = 200_000


class SheetLabeller:
    """State behind the page in criterion mode: the round's rows, labels and context."""

    def __init__(
        self,
        rows: Sequence[CriterionRow],
        criterion: Criterion,
        labels_path: Path,
        round_: Round,
        seed: int,
        context: Sequence[str] = (),
    ) -> None:
        if not rows:
            raise SheetError("empty sheet")
        self.items = second_pass(rows, seed=seed) if round_ == "relabel" else _first_pass(rows)
        self.criterion = criterion
        self.sheet_id = rows[0].sheet_id
        self.round: Round = round_
        self.context = tuple(context)
        existing = _load(labels_path)
        if existing is not None and (existing.sheet_id, existing.round) != (self.sheet_id, round_):
            raise SheetError(
                f"{labels_path} holds {existing.round}-round labels for another sheet or round; "
                f"use a separate file for the {round_} round"
            )
        for r in self.items:
            for p in self._paths(r):
                if not p.is_file():
                    raise SheetError(f"context file {p} does not exist")
        self.keys = {self._key(r) for r in self.items}  # fresh keys in the second pass
        self.path = labels_path
        self.labels: dict[str, str] = dict(existing.labels) if existing else {}
        self.lock = threading.Lock()
        self.port = 0

    def _paths(self, row: CriterionRow) -> list[Path]:
        out = []
        for template in self.context:
            try:
                out.append(Path(template.format_map(dict(row.ids))))
            except (KeyError, IndexError, ValueError):
                raise SheetError(
                    f"context template {template!r} names a field that is not one of "
                    f"{', '.join(self.criterion.id_fields) or 'no id fields'}"
                ) from None
        return out

    def _key(self, row: CriterionRow) -> str:
        return str(row.relabel_key) if self.round == "relabel" else row.key

    def view(self, index: int | None = None) -> dict[str, Any]:
        with self.lock:
            done = sum(1 for r in self.items if self._key(r) in self.labels)
            if index is None:
                index = next(
                    (i for i, r in enumerate(self.items) if self._key(r) not in self.labels),
                    len(self.items),
                )
            base = {
                "mode": "criterion",
                "criterion": self.criterion.name,
                "question": self.criterion.question,
                "keys": [{"key": k, "label": lab} for lab, k in self.criterion.keys.items()],
                "total": len(self.items),
                "labelled": done,
            }
            if index >= len(self.items):
                return {**base, "done": True}
            r = self.items[max(0, index)]
            context = [
                {"name": p.name, "text": p.read_text(encoding="utf-8")[:MAX_CONTEXT_CHARS]}
                for p in self._paths(r)
            ]
            return {
                **base,
                "done": False,
                "index": max(0, index),
                "key": self._key(r),
                "ids": dict(r.ids),
                "parts": list(r.parts),
                "context": context,
                "label": self.labels.get(self._key(r)),
            }

    def record(self, key: str, label: str, extraction_error: bool) -> None:
        with self.lock:
            if key not in self.keys:
                raise KeyError(key)
            if extraction_error:
                raise ValueError("criterion sheets have no extraction-error flag")
            canonical = self.criterion.parse(label)
            self.labels[key] = canonical
            _save(
                self.path,
                CriterionLabels(sheet_id=self.sheet_id, round=self.round, labels=self.labels),
            )


def serve_sheet_labeller(
    rows: Sequence[CriterionRow],
    criterion: Criterion,
    labels_path: Path,
    *,
    round_: Round = "first",
    seed: int = 0,
    context: Sequence[str] = (),
    port: int = 0,
) -> Any:
    """A2.6's labelling page on 127.0.0.1 in criterion mode (context pane, the criterion's
    keys). Returns the server; the caller runs `serve_forever()`."""
    from evalkit.pipeline.labeller import serve_state

    return serve_state(
        SheetLabeller(rows, criterion, labels_path, round_, seed, context), port=port
    )


# =============================== scoring (CIS-02, CIS-03) ===============================


class JudgeKappa(Model):
    judge: NonBlank
    n: NonNegInt
    kappa: Interval
    agreement: float
    confusion: tuple[tuple[NonNegInt, NonNegInt], tuple[NonNegInt, NonNegInt]]  # positive first


class Consistency(Model):
    n_repeats: NonNegInt
    self_agreement: float | None
    self_kappa: Interval | None
    n_planted: NonNegInt
    planted_correct: NonNegInt
    planted_accuracy: Interval | None
    passes: bool
    reasons: tuple[str, ...]


class IntegrityReport(Model):
    criterion: NonBlank
    rubric_hash: NonBlank
    run_hash: NonBlank
    scored_at: datetime
    n_heldout: NonNegInt
    heldout_digest: NonBlank
    agreements: tuple[JudgeKappa, ...]
    consistency: Consistency
    certified: bool
    reasons: tuple[str, ...]  # why not certified: the labeller's consistency, then the judges


def _wilson(k: int, n: int) -> Interval:
    p = k / n
    den = 1 + _Z95**2 / n
    centre = (p + _Z95**2 / (2 * n)) / den
    half = _Z95 * math.sqrt(p * (1 - p) / n + _Z95**2 / (4 * n * n)) / den
    return Interval(point=p, lo=min(p, max(0.0, centre - half)), hi=max(p, min(1.0, centre + half)))


def _consistency_reasons(
    bar: ConsistencyBar,
    n_repeats: int,
    self_kappa: Interval | None,
    n_planted: int,
    planted_accuracy: Interval | None,
) -> list[str]:
    """Why the labeller's consistency does not clear the bar (empty: it does). Pure: also
    recomputed from a logged `calibration_scored` line by `calibration_certified`."""

    def value(iv: Interval) -> float:
        return iv.lo if bar.bound == "lo" else iv.point

    reasons = []
    if n_repeats == 0:
        reasons.append("no blind repeats: certifying needs consistency evidence (n_relabel > 0)")
    if n_repeats < bar.min_repeats:
        reasons.append(f"{n_repeats} blind repeat(s); the bar needs {bar.min_repeats}")
    if self_kappa is None or value(self_kappa) < bar.min_self_kappa:
        reasons.append(f"self-agreement kappa below {bar.min_self_kappa} ({bar.bound})")
    if n_planted < bar.min_planted:
        reasons.append(f"{n_planted} planted item(s); the bar needs {bar.min_planted}")
    if planted_accuracy is None or value(planted_accuracy) < bar.min_planted_accuracy:
        reasons.append(f"planted accuracy below {bar.min_planted_accuracy} ({bar.bound})")
    return reasons


def _judge_reasons(bar: ConsistencyBar, kappa: Mapping[str, Interval]) -> list[str]:
    """Why the judges do not clear the bar: each judge's held-out kappa CI lower bound, which
    is never enough at or below 0 (no better than chance, such as a judge that always gives
    one label), whatever the bar."""
    if not kappa:
        return ["no judge kappa"]
    return [
        f"judge {j} kappa lower bound {kappa[j].lo:.2f} is below {bar.min_judge_kappa_lo}"
        for j in sorted(kappa)
        if kappa[j].lo < bar.min_judge_kappa_lo or kappa[j].lo <= 0
    ]


def consistency(
    rows: Sequence[CriterionRow],
    first: CriterionLabels,
    second: CriterionLabels | None,
    *,
    bar: ConsistencyBar,
    seed: int,
    positive: str,
    n_resamples: int = 10_000,
) -> Consistency:
    """Self-agreement on the blind repeats (kappa with a cluster bootstrap CI, one cluster per
    item) and accuracy on the planted items (Wilson 95%), against the bar (CIS-02)."""
    got = second.labels if second is not None else {}
    repeats = [
        r
        for r in rows
        if r.relabel and r.relabel_key in got and r.key in first.labels  # fresh second key
    ]
    pairs = [
        CalibrationPair(r.key, first.labels[r.key] == positive, got[str(r.relabel_key)] == positive)
        for r in repeats
    ]
    self_kappa = cluster_kappa(pairs, seed=seed, n_resamples=n_resamples) if pairs else None
    agree = sum(p.human == p.judge for p in pairs) / len(pairs) if pairs else None
    planted = [r for r in rows if r.split == "planted" and r.relabel_key in got]
    correct = sum(got[str(r.relabel_key)] == r.planted_label for r in planted)
    accuracy = _wilson(correct, len(planted)) if planted else None
    reasons = _consistency_reasons(bar, len(pairs), self_kappa, len(planted), accuracy)
    return Consistency(
        n_repeats=len(pairs),
        self_agreement=agree,
        self_kappa=self_kappa,
        n_planted=len(planted),
        planted_correct=correct,
        planted_accuracy=accuracy,
        passes=not reasons,
        reasons=tuple(reasons),
    )


def _norm_rubric(value: str) -> str:
    try:
        return H._rubric(value)
    except H.HeldoutError as exc:
        raise SheetError(str(exc)) from None


def _log_rubric(task_score: str, rubric: str) -> str:
    """The rubric under which B5.14's score-once guard records a calibration: namespaced by
    the task score being calibrated (what is scored, not the criterion's name), so calibrating
    never collides with scoring the same cases for a benchmark, and renaming a criterion
    cannot rescore a case."""
    return _norm_rubric(f"calibration:{task_score}:{rubric}")


def _digest(case_ids: Iterable[str]) -> str:
    return hashlib.sha256("\n".join(sorted(case_ids)).encode("utf-8")).hexdigest()


def _kappa(rows: Sequence[CriterionRow], first: CriterionLabels, items: Mapping[str, Item],
           judge: str, positive: str, seed: int, n_resamples: int) -> JudgeKappa:  # fmt: skip
    pairs = [
        CalibrationPair(
            r.key, first.labels[r.key] == positive, items[r.case_id].verdicts[judge] == positive
        )
        for r in rows
    ]
    tp, fn, fp, tn = _confusion(pairs)
    return JudgeKappa(
        judge=judge,
        n=len(pairs),
        kappa=cluster_kappa(pairs, seed=seed, n_resamples=n_resamples),
        agreement=(tp + tn) / len(pairs),
        confusion=((tp, fn), (fp, tn)),
    )


class CalibrationLine(Model):
    """The `calibration_scored` line in B5.14's log (CIS-02): ids as hashes, no text. It is
    appended in the same single write as its `heldout_scored` line, both carrying `pair` (the
    sha256 of this line's other fields), and carries the bar it was scored against and every
    value the bar reads, so `calibration_certified` recomputes the decision instead of
    trusting `certified`."""

    kind: Literal["calibration_scored"] = CALIBRATION_SCORED
    criterion: NonBlank
    task_score: NonBlank  # what is calibrated (the score-once key), not the criterion's name
    rubric_hash: NonBlank
    run_hash: NonBlank
    case_id_sha256: tuple[NonBlank, ...]
    heldout_digest: NonBlank
    certified: bool
    bar: FrozenMap[int | float | str | None]  # the ConsistencyBar, field by field
    kappa: FrozenMap[Interval]  # judge -> held-out kappa with its bootstrap CI
    n_heldout: NonNegInt
    n_repeats: NonNegInt
    self_kappa: Interval | None
    n_planted: NonNegInt
    planted_correct: NonNegInt
    planted_accuracy: Interval | None
    at: NonBlank
    pair: str = ""  # _pair_id of the other fields; also on its heldout_scored line


def _pair_id(body: Mapping[str, Any]) -> str:
    """The pair id of a calibration line: sha256 of its canonical JSON without `prev` and
    `pair`. Its `heldout_scored` line carries the same id, written in the same append."""
    return H._sha(H._canonical({k: v for k, v in body.items() if k not in ("prev", "pair")}))


def _line_reasons(line: CalibrationLine) -> list[str]:
    """Why a logged line does not certify, recomputed from its own values against its own
    bar (Wilson accuracy recomputed from the counts). A bar with a field missing, or below
    its floor, never certifies."""
    fields_: dict[str, Any] = dict(line.bar)
    problems = _bar_problems(fields_)
    if problems:
        return problems
    bar = ConsistencyBar(**fields_)
    if line.planted_correct > line.n_planted:
        return ["more planted items right than planted items"]
    accuracy = _wilson(line.planted_correct, line.n_planted) if line.n_planted else None
    if accuracy != line.planted_accuracy:
        return ["the logged planted accuracy does not match its counts"]
    if (line.n_repeats == 0) != (line.self_kappa is None):
        return ["the logged self-agreement does not match its count"]
    return [
        *_consistency_reasons(bar, line.n_repeats, line.self_kappa, line.n_planted, accuracy),
        *_judge_reasons(bar, line.kappa),
    ]


def _refuse_rubric_change(
    log_rows: Sequence[Mapping[str, Any]], task_score: str, rubric: str, hashes: Sequence[str]
) -> None:
    """RubricChanged when a held-out case was calibrated for this task score under another
    rubric, through any criterion name (every `calibration:<task_score>:*` line)."""
    prefix = f"calibration:{task_score}:"
    want = set(hashes)
    clash: set[str] = set()
    for row in log_rows:
        old = str(row.get("rubric_hash", "")).strip().lower()
        if row.get("kind") == H.SCORED and old.startswith(prefix):
            other = old[len(prefix) :] != rubric
        elif row.get("kind") == CALIBRATION_SCORED and row.get("task_score") == task_score:
            other = old != rubric
        else:
            continue
        if other:
            clash |= want & {str(h) for h in row.get("case_id_sha256") or ()}
    if clash:
        raise RubricChanged(
            f"{len(clash)} held-out case(s) were already calibrated for task score "
            f"{task_score} under another rubric (by any criterion); a changed rubric needs a "
            "fresh held-out set: " + ", ".join(sorted(h[:12] for h in clash))
        )


def _planted_not_heldout(
    planted: Iterable[tuple[str, Sequence[str]]],
    held_hashes: set[str],
    held_parts: set[tuple[str, ...]],
) -> None:
    """A planted item that is a held-out case (by id in the manifest, or by its exact text)
    is refused: planted items are written by hand, never taken from the held-out split."""
    for n, (pid, parts) in enumerate(planted, start=1):
        if H.case_id_hash(pid) in held_hashes:
            raise SheetError(
                f"planted item {n} is held-out case {H.manifest_id(pid)} in the manifest: "
                "write planted items by hand, never from the held-out split"
            )
        if tuple(parts) in held_parts:
            raise SheetError(
                f"planted item {n} repeats a held-out item's text: write planted items by "
                "hand, never from the held-out split"
            )


def score_sheet(
    rows: Sequence[CriterionRow],
    first: CriterionLabels,
    second: CriterionLabels | None,
    items: Sequence[Item],
    criterion: Criterion,
    *,
    rubric_hash: str,
    run_hash: str,
    manifest: Mapping[str, Any],
    log: str | Path,
    seed: int,
    scored_at: datetime,
    bar: ConsistencyBar | None = None,
    n_resamples: int = 10_000,
) -> IntegrityReport:
    """Score the held-out half once under this rubric and log it (see the spec for the order
    of checks). Nothing is logged unless every check passes."""
    bar = bar or criterion.bar
    rubric = _norm_rubric(rubric_hash)
    task_score = criterion.score_name
    log_rubric = _log_rubric(task_score, rubric)
    if not str(run_hash).strip():
        raise SheetError("a scoring needs the run hash")
    _check_labels(rows, criterion, first, second)
    held_hashes = _heldout_hashes(manifest)
    for r in _first_pass(rows):
        listed = H.case_id_hash(r.case_id) in held_hashes
        if listed != (r.split == "heldout"):
            raise SheetError(
                f"case {H.manifest_id(r.case_id)} is {'in' if listed else 'not in'} the "
                f"manifest's held-out split but sits in the {r.split} half of this sheet"
            )
    _planted_not_heldout(
        ((r.case_id.removeprefix("planted:"), r.parts) for r in rows if r.split == "planted"),
        held_hashes,
        {tuple(r.parts) for r in rows if r.split == "heldout"},
    )
    by_case = {it.case_id: it for it in items}
    for r in _first_pass(rows):
        it = by_case.get(r.case_id)
        if it is None or tuple(it.parts) != tuple(r.parts):
            raise SheetError(f"the item under key {r.key} changed since export: export again")
    heldout = [r for r in rows if r.split == "heldout"]
    if not heldout:
        raise SheetError("no held-out rows")
    stale = [r for r in _first_pass(rows) if by_case[r.case_id].verdicts and (
        by_case[r.case_id].rubric_hash is None
        or _norm_rubric(str(by_case[r.case_id].rubric_hash)) != rubric)]  # fmt: skip
    if stale:  # never stamp old verdicts with the current rubric
        raise SheetError(
            f"{len(stale)} verdict(s) carry no rubric hash or another rubric than the current "
            "one: re-judge the run before scoring"
        )
    judges = sorted(criterion.judges) or sorted(
        {j for r in _first_pass(rows) for j in by_case[r.case_id].verdicts}
    )
    if not judges:
        raise SheetError(f"no {criterion.name} judge verdicts on the sample")
    for j in judges:
        missing = [r for r in heldout if j not in by_case[r.case_id].verdicts]
        if missing:
            raise SheetError(f"{len(missing)} held-out row(s) have no verdict from judge {j}")
    agreements = tuple(
        _kappa(heldout, first, by_case, j, criterion.positive, seed, n_resamples) for j in judges
    )
    cons = consistency(
        rows, first, second, bar=bar, seed=seed, positive=criterion.positive,
        n_resamples=n_resamples,
    )  # fmt: skip
    floor = _bar_problems(asdict(bar))  # a bar forced below its floors never certifies
    reasons = tuple(floor) or (
        *cons.reasons,
        *_judge_reasons(bar, {a.judge: a.kappa for a in agreements}),
    )
    held_ids = sorted(r.case_id for r in heldout)
    hashes = sorted({H.case_id_hash(c) for c in held_ids})
    report = IntegrityReport(
        criterion=criterion.name,
        rubric_hash=rubric,
        run_hash=str(run_hash),
        scored_at=scored_at,
        n_heldout=len(heldout),
        heldout_digest=heldout_digest(rows),
        agreements=agreements,
        consistency=cons,
        certified=not reasons,
        reasons=reasons,
    )
    at = scored_at.date().isoformat()
    line = CalibrationLine(
        criterion=criterion.name,
        task_score=task_score,
        rubric_hash=rubric,
        run_hash=str(run_hash),
        case_id_sha256=tuple(hashes),
        heldout_digest=report.heldout_digest,
        certified=report.certified,
        bar=asdict(bar),
        kappa={a.judge: a.kappa for a in agreements},
        n_heldout=len(hashes),
        n_repeats=cons.n_repeats,
        self_kappa=cons.self_kappa,
        n_planted=cons.n_planted,
        planted_correct=cons.planted_correct,
        planted_accuracy=cons.planted_accuracy,
        at=at,
    )
    if _line_reasons(line) != list(reasons):  # the logged line must decide as the report
        raise SheetError("internal: the calibration line does not reproduce the report")
    body = line.model_dump(mode="json", exclude={"pair"})
    pair = _pair_id(body)
    scored = {
        "kind": H.SCORED,
        "rubric_hash": log_rubric,
        "case_id_sha256": hashes,
        "n_cases": len(hashes),
        "at": at,
        "pair": pair,
    }
    # ---- the log: one lock for the checks, and both lines in ONE write (both or neither) ----
    log_path = Path(log)
    with H._log_lock(log_path):
        if H.chain_problems(log_path):
            raise SheetError("the log's hash chain is broken: restore it before scoring")
        _refuse_rubric_change(H.read_log(log_path), task_score, rubric, hashes)
        # B5.14 score once (AlreadyScored) is checked on the heldout_scored line
        H._append_many_locked(log_path, [scored, {**body, "pair": pair}], doing="scoring")
    return report


def calibration_certified(
    log: str | Path,
    *,
    criterion: str | Criterion,
    case_ids: Iterable[str],
    rubric_hash: str,
    run_hash: str,
    task_score: str | None = None,
) -> bool:
    """True only when the log's chain and score-once rule hold and EXACTLY ONE
    `calibration_scored` line for this task score (`task_score`, else the criterion's) and
    rubric touches these held-out cases; that line directly follows (chained by hash) its
    `heldout_scored` line for exactly these cases, and both carry the same pair id (the hash
    of the calibration line's content), found nowhere else in the log: the two were written
    together; no line anywhere in the log calibrated any of these cases for this task score
    under another rubric (the rubric-change rule, re-applied here); the line names this
    criterion, run hash, case set and digest; it says `certified: true`; and recomputing the
    decision from its logged values against its logged bar (refused below `BAR_FLOORS`)
    certifies. A second matching line, or a matching line that says false, certifies
    nothing. Never raises. The last line of a log is chained by no later line, and a whole
    chain can be written by hand: protecting the log file itself is code review plus CI's
    append-only check (`verify-log`, B5.14)."""
    try:
        if isinstance(criterion, Criterion):
            name, ts = criterion.name, task_score or criterion.score_name
        else:
            name, ts = str(criterion), task_score or str(criterion)
        ids = sorted(set(H._case_ids(case_ids)))
        rubric = H._rubric(rubric_hash)
        want = sorted({H.case_id_hash(c) for c in ids})
        p = Path(log)
        if not want or not p.is_file() or H.chain_problems(p):
            return False
        raw = H._lines(p)
        rows = H.read_log(p)
        if len(raw) != len(rows):
            return False
        hits = [
            n
            for n, row in enumerate(rows)
            if row.get("kind") == CALIBRATION_SCORED
            and row.get("task_score") == ts
            and str(row.get("rubric_hash", "")).strip().lower() == rubric
            and {str(h) for h in row.get("case_id_sha256") or ()} & set(want)
        ]
        if len(hits) != 1 or hits[0] == 0:
            return False
        n = hits[0]
        before, row = rows[n - 1], rows[n]
        pair = row.get("pair")
        if (
            before.get("kind") != H.SCORED
            or before.get("rubric_hash") != _log_rubric(ts, rubric)
            or sorted(set(before.get("case_id_sha256") or ())) != want
            or row.get("prev") != H._sha(raw[n - 1])  # chained straight to its heldout line
            or not isinstance(pair, str)
            or before.get("pair") != pair  # ... and written with it
            or pair != _pair_id(row)
            or sum(r.get("pair") == pair for r in rows) != 2
        ):
            return False
        try:  # the rubric-change rule, re-applied to the whole log
            _refuse_rubric_change(rows, ts, rubric, want)
        except RubricChanged:
            return False
        body = {k: v for k, v in row.items() if k != "prev"}
        line = CalibrationLine.model_validate_json(json.dumps(body))
        return (
            line.criterion == name
            and line.run_hash == run_hash
            and list(line.case_id_sha256) == want
            and line.n_heldout == len(want)
            and line.heldout_digest == _digest(ids)
            and line.certified is True
            and not _line_reasons(line)
        )
    except Exception:  # fail closed
        return False


def run_hash(run_dir: str | Path) -> str:
    """sha256 of the run id, its config hash and the exact bytes of its records."""
    d = Path(run_dir)
    try:
        manifest = json.loads((d / "run.json").read_text(encoding="utf-8"))
        records = (d / "records.jsonl").read_bytes()
    except (OSError, ValueError) as exc:
        raise SheetError(f"{d} is not a finished run folder ({type(exc).__name__})") from None
    body = {
        "run_id": manifest.get("run_id"),
        "config_hash": manifest.get("config_hash"),
        "records_sha256": hashlib.sha256(records).hexdigest(),
    }
    return H._sha(H._canonical(body))


# =============================== run folder and CLI ===============================


def _dir(run_dir: Path, criterion: Criterion) -> Path:
    return run_dir / "calibration" / criterion.name


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _rows(run_dir: Path, criterion: Criterion) -> list[CriterionRow]:
    path = _dir(run_dir, criterion) / "sheet.json"
    if not path.is_file():
        raise SheetError(f"no {criterion.name} sheet yet: export it first")
    try:
        data = json.loads(path.read_text("utf-8"))
        if not isinstance(data, list):
            raise ValueError("not a list")
        return [CriterionRow.model_validate(x) for x in data]
    except (OSError, ValueError, TypeError):  # pydantic's error quotes the input: never shown
        raise SheetError(
            f"{path} is not a valid {criterion.name} sheet: export again (move {path.parent} "
            "aside first)"
        ) from None


def _write_relabel(run_dir: Path, criterion: Criterion, rows: Sequence[CriterionRow]) -> Path:
    """`relabel.csv`, the second-pass sheet: written only once the first pass is labelled."""
    from evalkit import run as R

    d = _dir(run_dir, criterion)
    first = _load(d / "labels-first.json")
    if not _first_complete(rows, first):
        done = len(first.labels) if first is not None else 0
        raise SheetError(
            f"finish the first pass before the second ({done} of {len(_first_pass(rows))} labelled)"
        )
    path = d / "relabel.csv"
    if not path.is_file():
        _write(path, sheet_csv(rows, criterion, second=True, seed=R.read_manifest(run_dir).seed))
    return path


def _manifest(path: str | Path | None) -> dict[str, Any]:
    if path is None:
        raise SheetError("--manifest (a B5.14 held-out manifest) is required")
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise SheetError(f"manifest {path} is unreadable or not JSON") from None
    if not isinstance(data, dict):
        raise SheetError(f"manifest {path} is not a JSON object")
    return data


def export_run(
    run_dir: Path,
    criterion: Criterion,
    *,
    manifest_path: str | Path | None,
    planted_path: str | Path | None = None,
    force: bool = False,
    model: str | None = None,
) -> tuple[Path, int, int]:
    """Write `sheet.json` (private) and `sheet.csv`. The second-pass `relabel.csv` is written
    only once the first pass is labelled (`import_run`, `label_run(relabel=True)`)."""
    from evalkit import run as R

    manifest = R.read_manifest(run_dir)
    if manifest.finished_at is None:
        raise SheetError("judging is still pending: finish the run before calibrating")
    d = _dir(run_dir, criterion)
    if (d / "sheet.json").is_file() and any(d.glob("labels-*.json")):
        raise SheetError(
            f"the {criterion.name} sheet already has labels that fit only it; move "
            f"{d} aside before a new export"
        )
    if (d / "sheet.json").is_file() and not force:
        raise SheetError(f"{d / 'sheet.json'} exists (use --force to replace it; no labels yet)")
    items = items_from_run(R.read_records(run_dir), R.read_cases(run_dir), criterion, model=model)
    held = _manifest(manifest_path)
    sample = sample_items(
        items, manifest=held, seed=manifest.seed, n=criterion.n, n_relabel=criterion.n_relabel,
    )  # fmt: skip
    planted = load_planted(planted_path, criterion) if planted_path else []
    hashes = _heldout_hashes(held)
    _planted_not_heldout(
        ((p.id, p.parts) for p in planted),
        hashes,
        {tuple(it.parts) for it in items if H.case_id_hash(it.case_id) in hashes},
    )
    rows = build_sheet(sample, items, criterion, seed=manifest.seed, planted=planted)
    (d / "relabel.csv").unlink(missing_ok=True)  # a re-export's second pass waits again
    _write(d / "sheet.json", json.dumps([r.model_dump(mode="json") for r in rows], indent=1) + "\n")
    _write(d / "sheet.csv", sheet_csv(rows, criterion))
    return d, len(_first_pass(rows)), len(second_pass(rows, seed=manifest.seed))


def label_run(
    run_dir: Path,
    criterion: Criterion,
    *,
    relabel: bool = False,
    context: Sequence[str] = (),
    port: int = 0,
) -> Any:
    from evalkit import run as R

    name = "labels-relabel.json" if relabel else "labels-first.json"
    rows = _rows(run_dir, criterion)
    if relabel:  # the second pass opens only after the first is labelled
        _write_relabel(run_dir, criterion, rows)
    return serve_sheet_labeller(
        rows,
        criterion,
        _dir(run_dir, criterion) / name,
        round_="relabel" if relabel else "first",
        seed=R.read_manifest(run_dir).seed,
        context=context,
        port=port,
    )


def import_run(run_dir: Path, criterion: Criterion, csv_path: Path, *, relabel: bool) -> int:
    round_: Round = "relabel" if relabel else "first"
    try:
        text = csv_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SheetError(f"cannot read {csv_path} ({type(exc).__name__})") from None
    rows = _rows(run_dir, criterion)
    labels = labels_from_csv(text, rows, criterion, round_=round_)
    if relabel:
        _write_relabel(run_dir, criterion, rows)  # refuses before the first pass is in
    target = _dir(run_dir, criterion) / f"labels-{round_}.json"
    existing = _load(target)
    if existing is not None and existing.labels:
        raise SheetError(f"{target} already holds labels; move it aside to import a CSV instead")
    _save(target, labels)
    if not relabel and _first_complete(rows, labels):
        _write_relabel(run_dir, criterion, rows)
    return len(labels.labels)


def score_run(
    run_dir: Path,
    criterion: Criterion,
    *,
    manifest_path: str | Path | None,
    log: str | Path | None,
    rubric_hash: str | None = None,
    now: datetime | None = None,
    model: str | None = None,
) -> IntegrityReport:
    from datetime import UTC

    from evalkit import run as R

    if log is None:
        raise SheetError("--log (the B5.14 hash-chained log) is required")
    rubric = rubric_hash or criterion.rubric_hash
    if not rubric:
        raise SheetError("the current rubric hash is required (--rubric-hash or rubric_hash:)")
    d = _dir(run_dir, criterion)
    rows = _rows(run_dir, criterion)
    first = _load(d / "labels-first.json")
    if first is None:
        raise SheetError(f"no first-round {criterion.name} labels yet")
    second = _load(d / "labels-relabel.json")
    manifest = R.read_manifest(run_dir)
    items = items_from_run(R.read_records(run_dir), R.read_cases(run_dir), criterion, model=model)
    path = d / "calibration.json"
    try:  # read before scoring: the log is written once, so this must not fail after it
        history = json.loads(path.read_text("utf-8")).get("history", []) if path.is_file() else []
        if not isinstance(history, list):
            raise ValueError("history is not a list")
    except (OSError, ValueError, AttributeError):
        raise SheetError(f"{path} is not a valid calibration file: move it aside") from None
    report = score_sheet(
        rows, first, second, items, criterion, rubric_hash=rubric, run_hash=run_hash(run_dir),
        manifest=_manifest(manifest_path), log=log, seed=manifest.seed,
        scored_at=now or datetime.now(UTC),
    )  # fmt: skip
    entry = report.model_dump(mode="json")
    _write(path, json.dumps({"current": entry, "history": [*history, entry]}, indent=1) + "\n")
    return report


def cli_calibrate(args: Any) -> int:
    """`evalkit calibrate ... --criterion FILE` (called from `evalkit.cli`)."""
    import sys

    from evalkit import run as R
    from evalkit.cli import _emit, _err

    run_dir = Path(args.run_dir)
    try:
        criterion = load_criterion(args.criterion)
        if args.action == "export":
            d, n1, n2 = export_run(
                run_dir, criterion, manifest_path=args.manifest, planted_path=args.planted,
                force=args.force, model=args.model,
            )  # fmt: skip
            _emit(
                args,
                {"dir": str(d), "first": n1, "second": n2},
                f"{n1} items to label, then {n2} in the second pass (relabel.csv is written "
                f"once the first pass is labelled). Label: evalkit calibrate label {run_dir} "
                f"--criterion {args.criterion}",
            )
        elif args.action == "label":
            server = label_run(
                run_dir, criterion, relabel=args.relabel, context=args.context or (),
                port=args.port,
            )  # fmt: skip
            url = f"http://127.0.0.1:{server.server_address[1]}/"
            _emit(args, {"url": url}, f"labelling page: {url}  (Ctrl-C to stop; labels autosave)")
            sys.stdout.flush()
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        elif args.action == "import":
            n = import_run(run_dir, criterion, Path(args.csv), relabel=args.relabel)
            second = _dir(run_dir, criterion) / "relabel.csv"
            ready = not args.relabel and second.is_file()
            _emit(
                args,
                {"imported": n, **({"relabel_csv": str(second)} if ready else {})},
                f"imported {n} labels" + (f"; the second pass is in {second}" if ready else ""),
            )
        else:
            report = score_run(
                run_dir, criterion, manifest_path=args.manifest, log=args.log,
                rubric_hash=args.rubric_hash, model=args.model,
            )  # fmt: skip
            kappas = {a.judge: a.kappa.point for a in report.agreements}
            c = report.consistency
            _emit(
                args,
                {
                    "kappa": kappas,
                    "self_kappa": c.self_kappa.point if c.self_kappa else None,
                    "planted_accuracy": c.planted_accuracy.point if c.planted_accuracy else None,
                    "certified": report.certified,
                    "reasons": list(report.reasons),
                },
                "held-out kappa: "
                + ", ".join(f"{j} {k:.2f}" for j, k in kappas.items())
                + f"\nlabeller: {c.n_repeats} repeats, {c.n_planted} planted"
                + (
                    "\ncertified"
                    if report.certified
                    else "\nNOT certified: " + "; ".join(report.reasons)
                ),
            )
    except (SheetError, H.HeldoutError, R.RunError, FileNotFoundError) as exc:
        return _err(str(exc))
    return 0
