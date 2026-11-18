"""Experiment ledger with a freeze guard (B5.7 LED-03; spec §3).

An experiment declares its hypothesis, change, primary metric and direction before any result
exists; it closes with compare's output (B5.6) and a verdict. Its frozen candidate gets one
look at the held-out split, reserved as a B5.14 `heldout_scored` line (`look:<candidate
sha256>`, every case of the split) BEFORE the split is scored, so a second look for the same
candidate is refused by B5.14's score-once rule (exit 1, nothing written). Held-out results are
stored only as split-level aggregates.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from evalkit import heldout as H
from evalkit.gates import GatesFile, canonical
from evalkit.ledger._io import (
    EXIT_ALARM,
    EXIT_INPUT,
    LEDGER_SCHEMA,
    LedgerError,
    append,
    canonical_json,
    check_project,
    guard_ids,
    held_splits,
    load_manifest,
    read_entries,
    sha256_text,
    text_field,
    utc_stamp,
)
from evalkit.ledger.runs import KEPT_HELDOUT, SETTINGS_IGNORED, _gates, load_run
from evalkit.metrics import NAME_RE, MetricsError, safe_text

Direction = Literal["up", "down"]
Verdict = Literal["supported", "refuted", "inconclusive"]
LOOK_PREFIX = "look:"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_EVALKIT_VERSION = re.compile(r"^\d+\.\d+$")
# a candidate is identified by at least one of these; the rest (project, data snapshot, the
# model list, judge mode) are hashed too but say nothing about which system it is
NOT_IDENTIFYING = frozenset({"project", "simulated", "models", "judge_mode", "snapshot"})
# the per-call settings an EvalKit record states that change a system's answers (a transport
# error, also in params, is an outcome, not a setting)
CALL_PARAMS = frozenset({"effort", "structured_output"})


# ---- the candidate (freeze guard) ---------------------------------------------------------------


def _run_folder_of(settings_file: Path) -> Path | None:
    """The run a `settings.json` belongs to: its folder, when that folder holds an EvalKit
    summary.json or a metrics.json (the run reads this settings.json as its own)."""
    from evalkit.runfolder import is_evalkit_file

    if settings_file.name != "settings.json":
        return None
    folder = settings_file.parent
    if is_evalkit_file(folder / "summary.json") or (folder / "metrics.json").is_file():
        return folder
    return None


def _evalkit_params(folder: Path) -> dict[str, str | None]:
    """Each model's call settings as its records state them (`effort`, `structured_output`),
    one setting per model: what of the run config, besides prompts and models, changes the
    system's answers. The run config's hash is not used: it covers the seed."""
    path = folder / "records.jsonl"
    if not path.is_file():
        return {}
    seen: dict[str, set[str]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        completion = json.loads(line).get("completion") or {}
        params = completion.get("params") or {}
        kept = {k: params[k] for k in sorted(CALL_PARAMS) if k in params}
        seen.setdefault(str(completion.get("model")), set()).add(canonical_json(kept))
    return {f"params.{m}": ", ".join(sorted(v)) for m, v in sorted(seen.items())}


def _run_settings(run: str | Path) -> dict[str, str | None]:
    loaded = load_run(run)
    settings = dict(loaded.settings)
    if loaded.source == "evalkit-summary":  # its config_hash covers the seed (RunConfig.seed)
        settings.pop("config_hash", None)
        settings.update(_evalkit_params(loaded.path.parent))
    return settings


def _candidate_settings(candidate: str | Path) -> dict[str, str | None]:
    from evalkit.compare import _read_settings

    p = Path(candidate)
    if p.is_file() and p.suffix == ".json":
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            raise LedgerError(f"{p.name} is unreadable or not JSON") from None
        version = data.get("schema_version") if isinstance(data, dict) else None
        is_run = version == "metrics-1" or (
            isinstance(version, str) and bool(_EVALKIT_VERSION.match(version))
        )
        if not is_run:
            folder = _run_folder_of(p)
            if folder is not None:  # a run's own settings.json names the run: one candidate
                return _run_settings(folder)
            try:
                settings = _read_settings(p)
            except MetricsError as exc:
                raise LedgerError("; ".join(exc.problems)) from None
            if settings.get("candidate_sha256") is None:
                raise LedgerError(
                    f"{p.name} is not beside a run and records no candidate_sha256: a settings "
                    "file on its own identifies a candidate only by its candidate_sha256 (or "
                    "name the run folder)"
                )
            return settings
    return _run_settings(candidate)


def candidate_hash(candidate: str | Path) -> str:
    """The frozen candidate's sha256: `candidate_sha256` from its settings.json when present
    (agentic-analytics' `fix_hash`, respectful-offers' challenger hash); otherwise sha256 of
    canonical JSON of every recorded setting except `seed`, `evalkit` and `results_format`, so a
    re-seed or an EvalKit upgrade is the same candidate. An EvalKit run's `config_hash` is left
    out (it covers the seed) and each model's call settings from its records are hashed
    instead; run ids, times and paths are never part of it. `candidate` is a run (folder,
    summary or metrics-1 file), the settings.json inside a run folder (the same candidate as
    the folder), or a settings JSON file on its own that records `candidate_sha256`. Raises
    (code 2) when none of these identifies it."""
    settings = _candidate_settings(candidate)
    given = settings.get("candidate_sha256")
    if given is not None:
        norm = given.strip().lower()
        if not _HEX64.fullmatch(norm):
            raise LedgerError("candidate_sha256 must be 64 hex characters")
        return norm
    keep = {k: v for k, v in settings.items() if k not in SETTINGS_IGNORED and v is not None}
    if not set(keep) - NOT_IDENTIFYING:
        raise LedgerError(
            "the candidate records nothing that identifies it (no candidate_sha256, code "
            "version, config or prompt hash, model ids, judges or settings.json key)"
        )
    return sha256_text(canonical_json(keep))


# ---- reading the ledger -------------------------------------------------------------------------


def _by_id(rows: Sequence[Mapping[str, Any]], exp_id: str) -> dict[str, list[Mapping[str, Any]]]:
    out: dict[str, list[Mapping[str, Any]]] = {"opened": [], "closed": [], "look": []}
    for r in rows:
        kind = str(r.get("kind", ""))
        if r.get("id") == exp_id and kind.startswith("experiment_"):
            out.setdefault(kind.removeprefix("experiment_"), []).append(r)
    return out


def _open(rows: Sequence[Mapping[str, Any]], exp_id: str) -> Mapping[str, Any]:
    found = _by_id(rows, exp_id)
    if not found["opened"]:
        raise LedgerError(f"no experiment {safe_text(exp_id)} in the ledger")
    if found["closed"]:
        raise LedgerError(f"experiment {safe_text(exp_id)} is already closed")
    return found["opened"][0]


def _ref(run: Any) -> dict[str, str]:
    return {"run_id": run.metrics.run_id, "summary_sha256": run.summary_sha256}


def _labels_text(labels: Mapping[str, Any] | None) -> dict[str, str]:
    return {str(k): canonical(v) for k, v in sorted((labels or {}).items())}


# ---- open ---------------------------------------------------------------------------------------


def open_experiment(
    ledger: str | Path,
    *,
    hypothesis: str,
    change: str,
    metric: str,
    direction: Direction,
    baseline: str | Path,
    labels: Mapping[str, Any] | None = None,
    manifest: str | Path | Mapping[str, Any] | None = None,
    cases: Mapping[str, str | Path] | None = None,
    heldout_splits: Sequence[Any] = (),
    at: str | None = None,
) -> dict[str, Any]:
    """Append `experiment_opened`: the hypothesis, the change, and the primary metric (one row of
    the baseline run, named by metric and labels) with the direction that would support it,
    declared before any result exists. The id is `E<n>` in ledger order."""
    if direction not in ("up", "down"):
        raise LedgerError("direction must be up or down")
    if not isinstance(metric, str) or not NAME_RE.match(metric):
        raise LedgerError("metric must match [a-z][a-z0-9_.]*")
    want = _labels_text(labels)
    run = load_run(baseline)
    named = [m for m in run.metrics.metrics if m.name == metric]
    if not want and len(named) == 1:  # one row of the metric: its labels are the primary's
        want = _labels_text(named[0].labels)
    if not any(_labels_text(m.labels) == want for m in named):
        hint = f" ({len(named)} label set(s) of it: name one with --labels)" if named else ""
        raise LedgerError(f"the baseline has no row {metric} with these labels{hint}")
    entry: dict[str, Any] = {
        "kind": "experiment_opened",
        "schema": LEDGER_SCHEMA,
        "at": utc_stamp(at),
        "project": run.metrics.project,
        "hypothesis": text_field("hypothesis", hypothesis, required=True),
        "change": text_field("change", change, required=True),
        "primary": {"metric": metric, "labels": want, "direction": direction},
        "baseline": _ref(run),
    }
    held = held_splits(None, heldout_splits)
    m = load_manifest(manifest)

    def check(rows: list[dict[str, Any]], e: dict[str, Any]) -> None:
        check_project(rows, e["project"], ledger)
        e["id"] = f"E{1 + sum(1 for r in rows if r.get('kind') == 'experiment_opened')}"
        guard_ids(e, m, held, cases)

    return append(ledger, entry, check=check)


# ---- look (the freeze guard) --------------------------------------------------------------------


def reserve_look(
    ledger: str | Path,
    exp_id: str,
    *,
    candidate: str | Path,
    manifest: str | Path,
    log: str | Path,
    split: str = "heldout",
    cases: Mapping[str, str | Path] | None = None,
    at: str | None = None,
) -> dict[str, Any]:
    """Freeze the candidate and reserve its one held-out look BEFORE the split is scored: a
    B5.14 `heldout_scored` line with rubric `look:<candidate sha256>` over every case of the
    split, then an `experiment_look` entry (no case hashes). A second look for the same
    candidate raises LedgerError with code 1 and writes nothing. The manifest is self-checked,
    and verified against the case files when `cases` ({split: path}) is given."""
    rows = read_entries(ledger)
    opened = _open(rows, exp_id)
    cand = candidate_hash(candidate)
    data = load_manifest(manifest)
    assert data is not None
    if cases:
        problems = H.verify_manifest(manifest, cases, id_field=str(data.get("id_field", "id")))
        if problems:
            raise LedgerError(f"manifest: {len(problems)} problem(s), first: {problems[0]}")
    splits = data["splits"]
    if split not in splits or not isinstance(splits[split], list) or not splits[split]:
        raise LedgerError(f"the manifest has no cases in split {safe_text(split)}")
    ids = sorted({str(e["id_sha256"]) for e in splits[split]})
    for r in rows:
        if r.get("kind") == "experiment_look" and r.get("candidate_sha256") == cand:
            raise LedgerError(
                f"candidate {cand[:12]} already had its held-out look ({r.get('id')}): "
                "a second look is refused",
                EXIT_ALARM,
            )
    stamp = utc_stamp(at)
    try:
        line = H.append_entry(
            log,
            {
                "kind": H.SCORED,
                "rubric_hash": LOOK_PREFIX + cand,
                "case_id_sha256": ids,
                "n_cases": len(ids),
                "at": stamp,
                "note": f"B5.7 experiment {exp_id}: the candidate's one held-out look",
            },
        )
    except H.AlreadyScored:
        raise LedgerError(
            f"candidate {cand[:12]} already had its held-out look on split {safe_text(split)}: "
            "a second look is refused",
            EXIT_ALARM,
        ) from None
    except H.HeldoutError as exc:
        raise LedgerError(f"held-out log: {exc}") from None
    entry: dict[str, Any] = {
        "kind": "experiment_look",
        "schema": LEDGER_SCHEMA,
        "at": stamp,
        "project": opened["project"],
        "id": exp_id,
        "candidate_sha256": cand,
        "manifest_sha256": str(data.get("manifest_sha256")),
        "split": split,
        "n_cases": len(ids),
        "log_line_sha256": sha256_text(canonical_json(line)),
    }

    def check(rows: list[dict[str, Any]], e: dict[str, Any]) -> None:
        _open(rows, exp_id)
        check_project(rows, e["project"], ledger)

    return append(ledger, entry, check=check)


# ---- close --------------------------------------------------------------------------------------


@dataclass
class ExperimentResult:
    entry: Mapping[str, Any]
    comparison: Any  # evalkit.compare.Comparison
    verdict: Verdict
    exit_code: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": "experiment-close-1",
            "id": self.entry.get("id"),
            "verdict": self.verdict,
            "exit_code": self.exit_code,
            "primary_row": self.entry.get("primary_row"),
            "heldout": self.entry.get("heldout"),
            "comparison": self.entry.get("comparison"),
        }


def verdict_for(change: Mapping[str, Any] | None, direction: str) -> Verdict:
    """supported: the primary row is flagged in the declared direction; refuted: flagged the
    other way; inconclusive: not flagged (including `too_few_cases` and `degenerate` rows)."""
    if not change or not change.get("flagged") or change.get("direction") not in ("up", "down"):
        return "inconclusive"
    return "supported" if change["direction"] == direction else "refuted"


def _primary_row(comparison: Any, primary: Mapping[str, Any]) -> dict[str, Any]:
    want = primary.get("labels") or {}
    for c in comparison.changes:
        if c.metric == primary["metric"] and dict(c.labels) == dict(want):
            return c.as_dict()
    raise LedgerError(
        f"the primary row {safe_text(primary['metric'])} is not compared in both runs "
        "(missing from one, or a held-out row folded with others)"
    )


def _heldout_block(
    rows: Sequence[Mapping[str, Any]], exp_id: str, result: str | Path, held: frozenset[str]
) -> dict[str, Any]:
    cand = candidate_hash(result)
    looks = [
        r
        for r in rows
        if r.get("kind") == "experiment_look"
        and r.get("id") == exp_id
        and r.get("candidate_sha256") == cand
    ]
    if not looks:
        raise LedgerError(
            f"no held-out look was reserved for candidate {cand[:12]} in {safe_text(exp_id)}: "
            "a held-out result is accepted only after `evalkit experiment look`"
        )
    for r in rows:
        block = r.get("heldout") if r.get("kind") == "experiment_closed" else None
        if isinstance(block, Mapping) and block.get("candidate_sha256") == cand:
            raise LedgerError(f"a held-out result for candidate {cand[:12]} is already recorded")
    look = looks[0]
    split = str(look["split"])
    run = load_run(result)
    kept, folded = [], 0
    for m in run.metrics.metrics:
        if "split" not in m.labels or canonical(m.labels["split"]) != split:
            continue
        if set(m.labels) <= KEPT_HELDOUT:
            kept.append(m.model_dump(exclude_none=True))
        else:
            folded += 1
    if not kept:
        raise LedgerError(
            f"the held-out result has no split-level rows for split {safe_text(split)} "
            "(labels within split, model, judge)"
        )
    return {
        "candidate_sha256": cand,
        "split": split,
        "manifest_sha256": look.get("manifest_sha256"),
        "run": {"run_id": run.metrics.run_id, "summary_sha256": run.summary_sha256},
        "metrics": kept,
        "heldout_rows_folded": folded,
    }


def close_experiment(
    ledger: str | Path,
    exp_id: str,
    *,
    before: str | Path,
    after: str | Path,
    gates: str | Path | GatesFile | None = None,
    manifest: str | Path | Mapping[str, Any] | None = None,
    cases: Mapping[str, str | Path] | None = None,
    heldout_splits: Sequence[Any] = (),
    heldout: str | Path | None = None,
    seed: int = 7,
    resamples: int = 10_000,
    at: str | None = None,
) -> ExperimentResult:
    """Run `compare.compare` on before and after and append `experiment_closed` with the
    comparison (compare-1, held-out rows already folded to split level), the primary row and
    the verdict. The exit code is compare's: 0, or 1 for a fail-gate regression (the entry is
    written for both); bad input raises (code 2) and writes nothing. With `heldout`, the
    candidate's held-out result is stored as split-level aggregates, only when a look was
    reserved for that candidate in this experiment and no result was recorded for it."""
    from evalkit.compare import compare_runs

    rows = read_entries(ledger)
    opened = _open(rows, exp_id)
    g = _gates(gates)
    m = load_manifest(manifest)
    held = held_splits(g, heldout_splits)
    c = compare_runs(
        before,
        after,
        gates=g,
        manifest=m,
        heldout_splits=tuple(heldout_splits),
        seed=seed,
        resamples=resamples,
    )
    if c.exit_code == EXIT_INPUT:
        raise LedgerError("compare: " + "; ".join(c.errors[:5]))
    primary = opened["primary"]
    row = _primary_row(c, primary)
    verdict = verdict_for(row, str(primary["direction"]))
    entry: dict[str, Any] = {
        "kind": "experiment_closed",
        "schema": LEDGER_SCHEMA,
        "at": utc_stamp(at),
        "project": opened["project"],
        "id": exp_id,
        "before": _ref(load_run(before)),
        "after": _ref(load_run(after)),
        "comparison": c.as_dict(),
        "primary_row": row,
        "verdict": verdict,
    }
    if heldout is not None:
        entry["heldout"] = _heldout_block(rows, exp_id, heldout, held)
    guard_ids(entry, m, held, cases)

    def check(rows: list[dict[str, Any]], e: dict[str, Any]) -> None:
        _open(rows, exp_id)
        check_project(rows, e["project"], ledger)

    written = append(ledger, entry, check=check)
    return ExperimentResult(written, c, verdict, c.exit_code)


# ---- show ---------------------------------------------------------------------------------------


def list_experiments(ledger: str | Path) -> dict[str, Any]:
    """Every experiment (id, hypothesis, primary, status, verdict) and the held-out looks spent
    per split, all candidates, so repeated new candidates are visible."""
    rows = read_entries(ledger)
    out: list[dict[str, Any]] = []
    looks: dict[str, list[dict[str, Any]]] = {}
    results = {
        r["heldout"]["candidate_sha256"]
        for r in rows
        if r.get("kind") == "experiment_closed" and isinstance(r.get("heldout"), Mapping)
    }
    for r in rows:
        if r.get("kind") == "experiment_opened":
            found = _by_id(rows, str(r.get("id")))
            closed = found["closed"][0] if found["closed"] else None
            out.append(
                {
                    "id": r.get("id"),
                    "at": r.get("at"),
                    "hypothesis": r.get("hypothesis"),
                    "change": r.get("change"),
                    "primary": r.get("primary"),
                    "status": "closed" if closed else "open",
                    "verdict": closed.get("verdict") if closed else None,
                    "looks": len(found["look"]),
                }
            )
        elif r.get("kind") == "experiment_look":
            looks.setdefault(str(r.get("split")), []).append(
                {
                    "id": r.get("id"),
                    "candidate_sha256": r.get("candidate_sha256"),
                    "n_cases": r.get("n_cases"),
                    "result_recorded": r.get("candidate_sha256") in results,
                }
            )
    return {
        "schema": "experiments-1",
        "experiments": out,
        "looks": {k: looks[k] for k in sorted(looks)},
    }
