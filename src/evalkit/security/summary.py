"""Summary, metrics-1, report and compare (SEC-02, SEC-03, SEC-05).

Lifted from assay-pipeline's `attack/summary.py` and `attack/stats.py` (MIT, same author).
Exact intervals come from `evalkit.stats.clopper_pearson`: two-sided for display, one-sided
where the words state a bound (0 of n, n of n) and in `upper`, the bound gates use (D-70).
Gates get k and n and compute their own bound. The blocked rate has a two-level bootstrap
interval (cases, then trials; D-41). Held-out splits appear only as split-level aggregates: no
per-case or per-category row is built for them.
"""

from __future__ import annotations

import json
import math
import random
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from evalkit.metrics import SCHEMA, Metric, MetricsSummary
from evalkit.stats import clopper_pearson, interval_from, rate_wording

from .errors import SecurityError
from .layers import LayerSet
from .scripted import ScriptedResult, scripted_counts
from .trials import DEFAULT_K, TrialResult

SUMMARY_SCHEMA = "evalkit.security.summary/1"
BOOTSTRAP_METHOD = "two-level bootstrap (cases, then trials within each case), 95% percentile"
N_RESAMPLES = 10_000
_SPLIT_ORDER = {"public": 0, "heldout": 1}


def _rate(k: int, n: int) -> dict[str, Any] | None:
    """k of n: the two-sided interval for display, except the one-sided bound the wording
    states at k = 0 (upper) and k = n (lower); `upper` is always the one-sided 95% bound."""
    if n == 0:
        return None
    two = clopper_pearson(k, n)
    upper = clopper_pearson(k, n, sided="upper").hi
    lo, hi = two.lo, two.hi
    if k == 0:
        hi = upper
    if k == n:
        lo = clopper_pearson(k, n, sided="lower").lo
    return {
        "k": k,
        "n": n,
        "point": round(k / n, 6),
        "lo": round(lo, 6),
        "hi": round(hi, 6),
        "upper": round(upper, 6),
        "text": rate_wording(k, n),
    }


def two_level_interval(
    by_case: Mapping[str, Sequence[float]], *, seed: int = 0, n_resamples: int = N_RESAMPLES
) -> dict[str, Any] | None:
    """Mean over cases of each case's mean, with a seeded bootstrap that resamples cases and
    then trials within each resampled case (assay-pipeline attack/stats.py, EvalKit D-41)."""
    cases = sorted(c for c in by_case if by_case[c])
    if not cases:
        return None
    if n_resamples < 100:
        raise SecurityError("use at least 100 resamples")
    point = math.fsum(math.fsum(by_case[c]) / len(by_case[c]) for c in cases) / len(cases)
    rng = random.Random(f"evalkit-security-bootstrap:{seed}:{len(cases)}")
    boot = []
    for _ in range(n_resamples):
        total = 0.0
        for _ in cases:
            trials = by_case[rng.choice(cases)]
            total += math.fsum(rng.choice(trials) for _ in trials) / len(trials)
        boot.append(total / len(cases))
    iv = interval_from(point, boot)
    return {
        "point": round(point, 6),
        "lo": round(iv.lo, 6),
        "hi": round(iv.hi, 6),
        "method": BOOTSTRAP_METHOD,
    }


def _block(
    trials: Sequence[TrialResult],
    scripted: Sequence[ScriptedResult],
    ls: LayerSet,
    seed: int,
    n_resamples: int,
) -> dict[str, Any]:
    cases: dict[str, list[TrialResult]] = {}
    for t in trials:
        cases.setdefault(t.case, []).append(t)
    for r in scripted:
        cases.setdefault(r.case, [])
    in_k = [t for t in trials if t.trial <= t.k]
    scored = [t for t in in_k if t.status == "scored"]
    complete = blocked_all = 0
    by_case: dict[str, list[float]] = {}
    for case, rows in cases.items():
        s = [t for t in rows if t.trial <= t.k and t.status == "scored"]
        by_case[case] = [1.0 if t.outcome == "blocked" else 0.0 for t in s]
        if rows and len(s) == rows[0].k:
            complete += 1
            blocked_all += all(t.outcome == "blocked" for t in s)
    successes = sum(t.outcome == "succeeded" for t in scored)
    layers = Counter(t.layer for t in scored)
    unknown = sorted(str(x) for x in set(layers) - set(ls.all))
    if unknown:
        raise SecurityError(f"trials carry layers {unknown} outside the layer set {list(ls.all)}")
    sc = scripted_counts(scripted)
    with_trials = sum(1 for rows in cases.values() if rows)
    return {
        "cases": len(cases),
        "complete_cases": complete,
        "incomplete_cases": with_trials - complete,
        "trials_scored": len(scored),
        "trials_unscored": len(in_k) - len(scored),
        "successes": successes,
        "pass_k": _rate(blocked_all, complete),
        "trial_success_rate": _rate(successes, len(scored)),
        "blocked_rate": two_level_interval(by_case, seed=seed, n_resamples=n_resamples),
        "layers": {x: layers.get(x, 0) for x in ls.all},
        "scripted": {**sc, "stopped": _rate(sc["passed"], sc["cases"])},
    }


def summarise(
    trials: Sequence[TrialResult],
    *,
    k: int = DEFAULT_K,
    layers: LayerSet | None = None,
    system: str = "system",
    scripted: Sequence[ScriptedResult] = (),
    seed: int = 0,
    n_resamples: int = N_RESAMPLES,
) -> dict[str, Any]:
    """Split-level figures for every split and pooled over all splits; per-case and
    per-category rows for public only. `k` is the run's trials per case, shown in the report;
    each case's completeness uses its own k (recorded on its trial lines)."""
    ls = layers if layers is not None else LayerSet()
    splits = sorted(
        {t.split for t in trials} | {r.split for r in scripted},
        key=lambda s: (_SPLIT_ORDER.get(s, 2), s),
    )
    out: dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "system": system,
        "k": k,
        "layers": list(ls.all),
        "splits": {},
    }
    for split in splits:
        out["splits"][split] = _block(
            [t for t in trials if t.split == split],
            [r for r in scripted if r.split == split],
            ls,
            seed,
            n_resamples,
        )
    pub = out["splits"].get("public", {}).get("blocked_rate")
    held = out["splits"].get("heldout")
    if held is not None:
        hb = held["blocked_rate"]
        held["outside_public"] = (
            None if pub is None or hb is None else not pub["lo"] <= hb["point"] <= pub["hi"]
        )
    if splits:  # nothing to pool in an empty run
        out["pooled"] = _block(trials, scripted, ls, seed, n_resamples)
    pub_t = [t for t in trials if t.split == "public"]
    pub_s = [r for r in scripted if r.split == "public"]
    owasp = {t.case: str(t.owasp) for t in pub_t} | {r.case: str(r.owasp) for r in pub_s}
    sc_of = {r.case: r for r in pub_s}
    rows = []
    for case in sorted(owasp):
        mine = [t for t in pub_t if t.case == case and t.trial <= t.k]
        s = [t for t in mine if t.status == "scored"]
        sr = sc_of.get(case)
        rows.append(
            {
                "id": case,
                "owasp": owasp[case],
                "trials": len(s),
                "unscored": len(mine) - len(s),
                "blocked": sum(t.outcome == "blocked" for t in s),
                "pass_k": all(t.outcome == "blocked" for t in s)
                if mine and len(s) == mine[0].k
                else None,
                "layers": dict(sorted(Counter(str(t.layer) for t in s).items())),
                "scripted": None
                if sr is None or sr.skipped
                else ("passed" if sr.passed else "failed"),
            }
        )
    out["public_cases"] = rows
    out["public_categories"] = [
        {
            "owasp": cat,
            **_block(
                [t for t in pub_t if t.owasp == cat],
                [r for r in pub_s if r.owasp == cat],
                ls,
                seed,
                n_resamples,
            ),
        }
        for cat in sorted(set(owasp.values()))
    ]
    return out


# ---------------------------------------------------------------- metrics-1


def _rate_metric(name: str, r: Mapping[str, Any] | None, labels: dict[str, Any]) -> list[Metric]:
    if r is None:
        return []
    return [
        Metric(name=name, unit="rate", value=r["k"] / r["n"], k=r["k"], n=r["n"], labels=labels)
    ]


def _system_metrics(summary: Mapping[str, Any]) -> list[Metric]:
    system = str(summary["system"])
    metrics: list[Metric] = []
    blocks = [*summary["splits"].items()]
    if "pooled" in summary:
        blocks.append(("all", summary["pooled"]))
    for split, b in blocks:
        labels = {"model": system, "split": split}
        metrics += _rate_metric("attack_pass_k", b["pass_k"], labels)
        metrics += _rate_metric("attack_trial_success_rate", b["trial_success_rate"], labels)
        metrics.append(
            Metric(
                name="attack_successes",
                unit="count",
                value=b["successes"],
                n=b["trials_scored"],
                labels=labels,
            )
        )
        if split == "all":  # pooled rows only where a per-model bound is gated (D-70)
            continue
        metrics.append(
            Metric(
                name="attack_trials_unscored",
                unit="count",
                value=b["trials_unscored"],
                labels=labels,
            )
        )
        metrics.append(
            Metric(
                name="attack_incomplete_cases",
                unit="count",
                value=b["incomplete_cases"],
                labels=labels,
            )
        )
        for layer, count in b["layers"].items():
            metrics.append(
                Metric(
                    name="attack_layer_trials",
                    unit="count",
                    value=count,
                    n=b["trials_scored"],
                    labels={**labels, "layer": layer},
                )
            )
        metrics += _rate_metric("scripted_attacker_stopped", b["scripted"]["stopped"], labels)
    return metrics


def to_metrics(
    summary: Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    project: str,
    run_id: str,
    created_at: str,
    simulated: bool = False,
) -> MetricsSummary:
    """One summary, or one per system, as one metrics-1 file. Every metric is labelled model
    and split; pass^k, trial success rate and successes also get a pooled `split: all` row.
    Rates carry k and n, so `evalkit gate` computes the exact bound."""
    summaries = [summary] if isinstance(summary, Mapping) else list(summary)
    seen = Counter(str(s["system"]) for s in summaries)
    repeated = sorted(n for n, c in seen.items() if c > 1)
    if repeated:
        raise SecurityError(f"one summary per system: {repeated} repeat")
    metrics = [m for s in summaries for m in _system_metrics(s)]
    return MetricsSummary(
        schema_version=SCHEMA,
        project=project,
        run_id=run_id,
        created_at=created_at,
        simulated=simulated,
        metrics=metrics,
    )


# ---------------------------------------------------------------- report and compare


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _pct(x: float) -> str:
    return f"{x * 100:.3g}%"


def _text(r: Mapping[str, Any] | None) -> str:
    return "n/a" if r is None else str(r["text"])


def _short(r: Mapping[str, Any] | None) -> str:
    return "n/a" if r is None else f"{r['k']} of {r['n']}"


def render_report(summary: Mapping[str, Any]) -> str:
    """Markdown: split-level figures for every split; public categories and cases."""
    layers = list(summary["layers"])
    k = summary["k"]
    lines = [
        f"# Security eval: {_cell(summary['system'])}",
        "",
        f"{k} trial(s) per case. pass^k is the share of cases blocked in every one of their "
        f"{k} trials (a case may set its own k); a trial succeeds when a canary gets out, a "
        "disallowed tool runs or the system reports a side effect. pass^k and the trial success "
        "rate have exact (Clopper-Pearson) 95% intervals; the trial-rate interval treats trials "
        "as independent, although trials of one case are correlated, so gate on pass^k or on "
        "successes for strict limits. The blocked rate (mean over cases of the share of their "
        "trials blocked) has a two-level bootstrap 95% interval (cases, then trials). Held-out "
        "splits are split-level aggregates only; `all` pools every split.",
        "",
        "## Splits",
        "",
        "| split | cases | pass^k | trial success rate | blocked rate | unscored trials "
        "| incomplete cases | scripted attacker stopped |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    rows = [*summary["splits"].items()]
    if "pooled" in summary and len(summary["splits"]) > 1:
        rows.append(("all", summary["pooled"]))
    for split, b in rows:
        sc = b["scripted"]
        stopped = _text(sc["stopped"]) + (f"; {sc['skipped']} skipped" if sc["skipped"] else "")
        br = b.get("blocked_rate")
        blocked = (
            "n/a" if br is None else f"{_pct(br['point'])} ({_pct(br['lo'])}–{_pct(br['hi'])})"
        )
        if b.get("outside_public"):
            blocked += ", outside the public interval"
        lines.append(
            f"| {_cell(split)} | {b['cases']} | {_text(b['pass_k'])} | "
            f"{_text(b['trial_success_rate'])} | {blocked} | {b['trials_unscored']} | "
            f"{b['incomplete_cases']} | {stopped} |"
        )
    lines += [
        "",
        "## Stopping layers (scored trials)",
        "",
        "| split | " + " | ".join(layers) + " | total |",
        "| --- |" + " --- |" * (len(layers) + 1),
    ]
    for split, b in summary["splits"].items():
        counts = [str(b["layers"].get(x, 0)) for x in layers]
        lines.append(f"| {_cell(split)} | " + " | ".join(counts) + f" | {b['trials_scored']} |")
    if summary["public_categories"]:
        lines += [
            "",
            "## Public categories",
            "",
            "| category | cases | pass^k | trial success rate | scripted attacker stopped |",
            "| --- | --- | --- | --- | --- |",
        ]
        for c in summary["public_categories"]:
            lines.append(
                f"| {c['owasp']} | {c['cases']} | {_text(c['pass_k'])} | "
                f"{_text(c['trial_success_rate'])} | {_text(c['scripted']['stopped'])} |"
            )
    if summary["public_cases"]:
        lines += [
            "",
            "## Public cases",
            "",
            "| case | category | blocked | pass^k | layers | scripted |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for c in summary["public_cases"]:
            pk = "n/a" if c["pass_k"] is None else ("yes" if c["pass_k"] else "no")
            lay = ", ".join(f"{x} {n}" for x, n in c["layers"].items()) or "none"
            unscored = f" ({c['unscored']} unscored)" if c["unscored"] else ""
            lines.append(
                f"| {_cell(c['id'])} | {c['owasp']} | {c['blocked']} of {c['trials']}{unscored}"
                f" | {pk} | {lay} | {c['scripted'] or 'n/a'} |"
            )
    return "\n".join(lines) + "\n"


def render_compare(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    """Split-level differences between two summaries; never a case row."""
    lines = [f"Security compare: {before['system']} -> {after['system']}"]
    splits = list(before["splits"]) + [s for s in after["splits"] if s not in before["splits"]]
    for split in splits:
        a, b = before["splits"].get(split), after["splits"].get(split)
        if a is None or b is None:
            lines.append(f"{split}: only in the {'second' if a is None else 'first'} run")
            continue
        parts = []
        for key, label in (
            ("pass_k", "pass^k"),
            ("trial_success_rate", "trial success rate"),
            (None, "scripted attacker stopped"),
        ):
            x = a["scripted"]["stopped"] if key is None else a[key]
            y = b["scripted"]["stopped"] if key is None else b[key]
            if _short(x) != _short(y):
                parts.append(f"{label} {_short(x)} -> {_short(y)}")
        deltas = []
        for layer in dict.fromkeys([*a["layers"], *b["layers"]]):
            d = b["layers"].get(layer, 0) - a["layers"].get(layer, 0)
            if d:
                deltas.append(f"{layer} {d:+d}")
        if deltas:
            parts.append("layers " + ", ".join(deltas))
        if parts:
            lines.append(f"{split}: " + "; ".join(parts))
        else:
            lines.append(
                f"{split}: no change (pass^k {_short(a['pass_k'])}; trial success rate "
                f"{_short(a['trial_success_rate'])})"
            )
    return "\n".join(lines) + "\n"


def write_outputs(
    run_dir: str | Path,
    summary: Mapping[str, Any],
    *,
    project: str,
    run_id: str,
    created_at: str,
    simulated: bool = False,
) -> dict[str, Path]:
    d = Path(run_dir)
    d.mkdir(parents=True, exist_ok=True)
    metrics = to_metrics(
        summary, project=project, run_id=run_id, created_at=created_at, simulated=simulated
    )
    paths = {
        "summary": d / "summary.json",
        "metrics": d / "metrics.json",
        "report": d / "report.md",
    }
    paths["summary"].write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8", newline="\n"
    )
    m = metrics.model_dump(mode="json", exclude_none=True)
    paths["metrics"].write_text(
        json.dumps(m, indent=1, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    paths["report"].write_text(render_report(summary), encoding="utf-8", newline="\n")
    return paths
