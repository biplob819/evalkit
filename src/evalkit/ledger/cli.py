"""`evalkit history`, `evalkit experiment`, `evalkit drift` and `evalkit spend` (B5.7), and the
Trend section of `evalkit report --history`. No model call, no network, no keys.

Exit codes, precedence 2 > 3 > 4 > 1 > 0 wherever more than one applies (GAT-09): 0 pass; 1 an
alarm (history, drift), a fail-gate regression (experiment close), a refused second look
(experiment look) or a broken chain (history verify); 2 bad input; 3 the cost cap (spend check;
a run stopped at its cap in history add or check); 4 judging pending (history add or check).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from evalkit.ledger._io import (
    EXPERIMENTS_PATH,
    RUNS_PATH,
    SPEND_PATH,
    LedgerError,
    guard_ids,
    held_splits,
    load_manifest,
)

LEDGER_EXIT_CODES = """exit codes (precedence 2 > 3 > 4 > 1 > 0):
  0  pass (warns are printed and in the JSON)
  1  an alarm (history, drift), a fail-gate regression (experiment close), a refused second
     held-out look (experiment look), a broken chain or append-only problem (history verify)
  2  bad input: an unreadable or invalid file, a run already recorded, an edited reference
  3  cost cap: spend reached the cap or the estimate would exceed it (spend check); a run
     stopped at its cost cap (history add, recorded; history check)
  4  judging pending (history add, not recorded; history check)"""


CASES_HELP = (
    "with --manifest: the held-out case files (checked against it), so free text quoting a "
    "held-out case is refused too"
)


def _print(args: argparse.Namespace, payload: dict[str, Any], text: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, sort_keys=True, default=str))
    else:
        print(text)


def _fail(args: argparse.Namespace, code: int, message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    if getattr(args, "json", False):
        print(json.dumps({"exit_code": code, "error": message}, sort_keys=True))
    return code


def _kv(values: Sequence[str] | None, flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in values or ():
        key, sep, value = v.partition("=")
        if not sep or not key:
            raise LedgerError(f"{flag} takes KEY=VALUE, got {v!r}")
        out[key] = value
    return out


def _cases(args: argparse.Namespace) -> dict[str, Path] | None:
    """`--cases SPLIT=PATH ...`: held-out case files, so free text quoting them is refused."""
    return {k: Path(v) for k, v in _kv(getattr(args, "cases", None), "--cases").items()} or None


def _write(path: str | Path, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _dump(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, indent=2) + "\n"


def _labels(labels: dict[str, str]) -> str:
    return ", ".join(f"{k}={v}" for k, v in labels.items()) or "-"


def _fmt(v: Any) -> str:
    from evalkit.ledger.chart import num

    return num(v)


# ---- history ------------------------------------------------------------------------------------


def _band_lines(bands: Sequence[Any]) -> list[str]:
    out = []
    for b in bands:
        if b.status in ("ok", "learning", "degenerate"):  # degenerate: unchanged, never flags
            continue
        d = b.as_dict()
        hints = f" [{', '.join(b.hints)}]" if b.hints else ""
        band = (
            f" (centre {_fmt(b.centre)}, spread {_fmt(b.spread)})" if b.centre is not None else ""
        )
        out.append(
            f"  {b.status:<10} {b.metric} {_labels(d['labels'])}: {_fmt(b.value)}{band}{hints}"
        )
    return out


def _check_text(res: Any, verb: str) -> str:
    d = res.as_dict()
    head = f"{verb} run {d['run_id']} ({d['status']}): {d['alarms']} alarm(s), {d['warns']} warn(s)"
    if d["learning"]:
        head += f", {d['learning']} row(s) still learning (fewer than 5 comparable runs)"
    if d["heldout_rows_folded"]:
        head += f"; {d['heldout_rows_folded']} held-out row(s) kept only at split level"
    return "\n".join([head, *_band_lines(res.bands)])


def cmd_history(args: argparse.Namespace) -> int:
    from evalkit.ledger import runs

    try:
        if args.action == "init":
            from evalkit.ledger._io import init_ledger

            made = init_ledger(args.ledger)
            word = "created empty ledger" if made else "ledger already exists, unchanged:"
            _print(
                args,
                {"exit_code": 0, "ledger": args.ledger, "created": made},
                word + " " + args.ledger,
            )
            return 0
        if args.action == "add":
            res = runs.record_run(
                args.ledger,
                args.run,
                gates=args.gates,
                heldout_splits=tuple(args.heldout_split or ()),
                manifest=args.manifest,
                cases=_cases(args),
                note=args.note,
            )
            _print(args, res.as_dict(), _check_text(res, f"recorded in {args.ledger}:"))
            return res.exit_code
        if args.action == "check":
            res = runs.check_run(
                args.ledger,
                args.run,
                gates=args.gates,
                heldout_splits=tuple(args.heldout_split or ()),
            )
            _print(args, res.as_dict(), _check_text(res, "checked (not recorded):"))
            return res.exit_code
        if args.action == "show":
            return _show(args)
        if args.action == "reset":
            entry = runs.reset_band(
                args.ledger,
                metric=args.metric,
                labels=_kv(args.labels, "--labels"),
                reason=args.reason,
                project=args.project,
            )
            _print(
                args,
                {"exit_code": 0, "entry": entry},
                f"band reset for {args.metric} in {args.ledger}",
            )
            return 0
        return _verify(args)
    except LedgerError as exc:
        return _fail(args, exc.code, str(exc))


def _show(args: argparse.Namespace) -> int:
    from evalkit.ledger.chart import history_html, history_markdown
    from evalkit.ledger.runs import history

    hist = history(
        args.ledger,
        gates=args.gates,
        metric=args.metric,
        heldout_splits=tuple(args.heldout_split or ()),
    )
    if args.md:
        _write(args.md, history_markdown(hist))
    if args.html:
        _write(args.html, history_html(hist))
    lines = [f"{hist.project or '(empty ledger)'}: {hist.runs} run(s)"]
    for s in hist.series:
        b = s.latest
        lines.append(
            f"  {b.status:<10} {s.metric} {_labels(b.as_dict()['labels'])}: latest "
            f"{_fmt(b.value)} over {len(s.points)} run(s), centre {_fmt(b.centre)}, spread "
            f"{_fmt(b.spread)}" + (f" [{', '.join(b.hints)}]" if b.hints else "")
        )
    if hist.spread:
        lines.append("efficiency spread (model, figure: median, MAD spread, CV, min-max, latest):")
        for r in hist.spread:
            lines.append(
                f"  {r.model} {r.name}: {_fmt(r.median)}, {_fmt(r.mad_spread)}, {_fmt(r.cv)}, "
                f"{_fmt(r.min)}-{_fmt(r.max)}, {_fmt(r.latest)} ({r.status})"
            )
    payload = {"exit_code": 0, **hist.as_dict()}
    _print(args, payload, "\n".join(lines))
    return 0


def _verify(args: argparse.Namespace) -> int:
    from evalkit import heldout as H
    from evalkit.ledger._io import verify_ledger

    files = [Path(f) for f in args.files] or [
        p for p in (RUNS_PATH, EXPERIMENTS_PATH, SPEND_PATH) if p.is_file()
    ]
    if not files:
        raise LedgerError("no ledger to verify: name one, or run from the project folder")
    report: dict[str, list[str]] = {}
    append_only: dict[str, str] = {}
    for f in files:
        revs: list[str] | None = None if args.chain_only else list(args.rev)
        tip = None
        if not args.chain_only and (args.base is not None or args.default_branch):
            top = H._Git(f.resolve().parent).toplevel()
            if top is None:
                raise LedgerError(f"{f}: not in a git work tree")
            try:
                found, tip = H.ci_revisions(top, base=args.base, default_branch=args.default_branch)
            except H.HeldoutError as exc:
                raise LedgerError(str(exc)) from None
            revs = [*(revs or []), *found]
        report[str(f)] = verify_ledger(f, revs, default_tip=tip)
        append_only[str(f)] = (
            f"checked against {len(revs)} revision(s)"
            if revs
            else "not checked: a chain cannot show lines cut from its end; only the git "
            "append-only check (--base, --rev or --default-branch) catches that"
        )
    bad = sum(len(v) for v in report.values())
    lines = []
    for f, problems in report.items():
        lines += [f"{f}: {p}" for p in problems]
        lines.append(f"{f}: OK" if not problems else f"{f}: {len(problems)} problem(s)")
        lines.append(f"{f}: append-only: {append_only[f]}")
    code = 1 if bad else 0
    payload = {"exit_code": code, "ledgers": report, "append_only": append_only}
    _print(args, payload, "\n".join(lines))
    return code


# ---- experiments --------------------------------------------------------------------------------


def cmd_experiment(args: argparse.Namespace) -> int:
    from evalkit.ledger import experiments as X

    try:
        if args.action == "open":
            entry = X.open_experiment(
                args.experiments,
                hypothesis=args.hypothesis,
                change=args.change,
                metric=args.metric,
                labels=_kv(args.labels, "--labels"),
                direction=args.direction,
                baseline=args.baseline,
                manifest=args.manifest,
                cases=_cases(args),
            )
            _print(
                args,
                {"exit_code": 0, "entry": entry},
                f"opened {entry['id']}: {entry['primary']['metric']} expected "
                f"{entry['primary']['direction']}",
            )
            return 0
        if args.action == "close":
            res = X.close_experiment(
                args.experiments,
                args.id,
                before=args.before,
                after=args.after,
                gates=args.gates,
                manifest=args.manifest,
                cases=_cases(args),
                heldout_splits=tuple(args.heldout_split or ()),
                heldout=args.heldout,
                seed=args.seed,
                resamples=args.resamples,
            )
            row = res.entry["primary_row"]
            text = (
                f"closed {args.id}: {res.verdict} ({row['metric']} change {_fmt(row['diff'])}, "
                f"95% CI {_fmt(row['ci_low'])} to {_fmt(row['ci_high'])}, "
                f"{'flagged' if row['flagged'] else 'not flagged'})"
            )
            if res.entry.get("heldout"):
                text += f"; held-out result recorded for split {res.entry['heldout']['split']}"
            _print(args, res.as_dict(), text)
            return res.exit_code
        if args.action == "look":
            cases = _kv(args.cases, "--cases")
            entry = X.reserve_look(
                args.experiments,
                args.id,
                candidate=args.candidate,
                manifest=args.manifest,
                log=args.log,
                split=args.split,
                cases={k: Path(v) for k, v in cases.items()} or None,
            )
            _print(
                args,
                {"exit_code": 0, "entry": entry},
                f"{args.id}: held-out look reserved for candidate "
                f"{entry['candidate_sha256'][:12]} on split {entry['split']} "
                f"({entry['n_cases']} cases); score the split now, once",
            )
            return 0
        shown = X.list_experiments(args.experiments)
        lines = []
        for e in shown["experiments"]:
            lines.append(
                f"{e['id']} {e['status']}"
                + (f" ({e['verdict']})" if e["verdict"] else "")
                + f": {e['primary']['metric']} {e['primary']['direction']}; {e['looks']} look(s)"
            )
        for split, looks in shown["looks"].items():
            lines.append(f"looks spent on {split}: {len(looks)} candidate(s)")
        _print(args, {"exit_code": 0, **shown}, "\n".join(lines) or "no experiments yet")
        return 0
    except LedgerError as exc:
        return _fail(args, exc.code, str(exc))


# ---- drift --------------------------------------------------------------------------------------


def _drift_guard(args: argparse.Namespace, payload: Any) -> None:
    m = load_manifest(args.manifest)
    if m is not None:
        guard_ids({"output": payload}, m, held_splits(None, tuple(args.heldout_split or ())))


def cmd_drift(args: argparse.Namespace) -> int:
    from evalkit import drift as D

    try:
        if args.action == "reference":
            source = args.data or args.metrics
            if not source or (args.data and args.metrics):
                raise LedgerError("give --data (rows) or --metrics (a metrics-1 file)")
            ref = D.freeze_reference(
                source,
                features=[f for f in (args.features or "").split(",") if f],
                prob=args.prob,
                label=args.label,
                categorical=[f for f in (args.categorical or "").split(",") if f],
                calibration_prefix=args.calibration_prefix,
                seed=args.seed,
                resamples=args.resamples,
            )
            _drift_guard(args, ref)
            _write(args.out, _dump(ref))
            _print(
                args,
                {"exit_code": 0, "reference": str(args.out), "profile": ref["profile_sha256"]},
                f"reference {args.out}: {len(ref['features'])} feature(s)"
                + (", calibration" if "calibration" in ref else "")
                + f"; profile_sha256 {ref['profile_sha256']}",
            )
            return 0
        source = args.data or args.metrics
        if not source or (args.data and args.metrics):
            raise LedgerError("give --data (rows) or --metrics (a metrics-1 file)")
        report = D.check(
            args.reference, source, config=args.config, seed=args.seed, resamples=args.resamples
        )
        payload = report.as_dict()
        _drift_guard(args, payload)
        emitted = None
        if args.emit_metrics:
            if not args.project:
                raise LedgerError("--emit-metrics needs --project")
            run_id = args.run_id or f"drift-{report.new['source_sha256'][:12]}"
            emitted = D.to_metrics(report, project=args.project, run_id=run_id, prefix=args.prefix)
            _drift_guard(args, emitted)
        if args.out:
            _write(args.out, _dump(payload))
        if emitted is not None:
            _write(args.emit_metrics, _dump(emitted))
        lines = [
            f"  {f['level']:<6} {f['feature']}: PSI {f['psi_shown']}"
            + (" (small batch)" if f["small_batch"] else "")
            for f in payload["features"]
        ]
        cal = payload["calibration"]
        if cal:
            for d in cal["deltas"]:
                lines.append(
                    f"  {d['status']:<6} {d['name']} change {_fmt(d['delta'])} (95% CI "
                    f"{_fmt(d['ci_low'])} to {_fmt(d['ci_high'])}, tolerance {d['tolerance']})"
                )
            if cal["bins_status"] == "bins_not_aligned":
                lines.append("  reliability bins do not align: per-bin drift not available")
        head = (
            f"drift: {payload['verdict']} ({payload['alarms']} alarm(s), "
            f"{payload['warns']} warn(s))"
        )
        _print(args, payload, "\n".join([head, *lines]))
        return report.exit_code
    except (LedgerError, D.DriftError) as exc:
        return _fail(args, getattr(exc, "code", 2), str(exc))


# ---- spend --------------------------------------------------------------------------------------


def cmd_spend(args: argparse.Namespace) -> int:
    from evalkit.ledger import spend as S
    from evalkit.ledger._io import project_of, read_entries

    try:
        if args.action == "add":
            project = args.project
            if project is None and Path(args.spend_ledger).is_file():
                with contextlib.suppress(LedgerError):  # a legacy-only ledger: name it
                    project = project_of(read_entries(args.spend_ledger))
            if not project:
                raise LedgerError("name the project with --project (the ledger has none yet)")
            entry = S.record_spend(
                args.spend_ledger,
                spend_kind=args.kind,
                spent_usd=args.usd,
                project=project,
                run_id=args.run_id,
                model=args.model,
                n_cases=args.n_cases,
                note=args.note,
            )
            total = S.running_total(args.spend_ledger)
            _print(
                args,
                {"exit_code": 0, "entry": entry, "running_total_usd": total},
                f"recorded ${entry['spent_usd']:.6f} ({entry['spend_kind']}); all spend so far "
                f"${total:.2f}",
            )
            return 0
        status = S.check_spend(
            args.spend_ledger,
            cap=args.cap,
            window=args.window,
            alarm_fraction=args.alarm_fraction,
            add_estimate=args.add_estimate,
            field_map=_kv(args.map, "--map"),
        )
        d = status.as_dict()
        word = {"ok": "ok", "alarm": "ALARM", "cap": "CAP REACHED"}[status.status]
        text = (
            f"spend {word}: ${status.spent_usd:.2f} "
            + ("in total" if status.window == "total" else f"this UTC {status.window}")
            + (f" + ${status.estimate_usd:.2f} estimate" if status.estimate_usd else "")
            + f" of a ${status.cap_usd:.2f} cap (alarm at ${status.alarm_at_usd:.2f}, "
            f"{status.alarm_fraction:g} of the cap); ledger {status.chain}"
        )
        _print(args, d, text)
        return status.exit_code
    except LedgerError as exc:
        return _fail(args, exc.code, str(exc))


# ---- the report's Trend section -----------------------------------------------------------------


def add_trend_to_report(report: Path, run: str | Path, ledger: str | Path) -> int:
    """Insert the Trend section (charts for the metrics the run reports, held-out at split
    level, and the PSI panel) into a rendered report.html. Returns the number of charts."""
    from evalkit.ledger.chart import CSS, trend_section
    from evalkit.ledger.runs import fold_rows, history, label_key, load_run

    hist = history(ledger)
    rows, _folded, _dates = fold_rows(load_run(run).metrics, held_splits())
    keys = {(r["name"], label_key(r["labels"])) for r in rows}
    chosen = [s for s in hist.series if (s.metric, label_key(s.labels)) in keys]
    section = f"<style>{CSS}</style>" + trend_section(hist, chosen)
    html = report.read_text(encoding="utf-8")
    marker = "</main><script>"
    if html.count(marker) != 1:
        raise LedgerError(f"{report.name}: no place for the Trend section")
    _write(report, html.replace(marker, section + marker))
    return len(chosen)


# ---- the parser ---------------------------------------------------------------------------------


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", action="store_true", help="print one JSON object")


def add_commands(sub: Any) -> None:
    """Register the four command groups on `evalkit`'s subparsers."""
    fmt = argparse.RawDescriptionHelpFormatter
    h = sub.add_parser(
        "history",
        help="run history with alarm bands (B5.7; no model calls)",
        epilog=LEDGER_EXIT_CODES,
        formatter_class=fmt,
    )
    hs = h.add_subparsers(dest="action", required=True)
    for name, helptext in (
        ("add", "record a run (once) and judge it against its history"),
        ("check", "judge a run against the history without recording it"),
    ):
        a = hs.add_parser(name, help=helptext, epilog=LEDGER_EXIT_CODES, formatter_class=fmt)
        a.add_argument("run", help="a metrics-1 file, an EvalKit run folder or its summary.json")
        a.add_argument("--ledger", default=str(RUNS_PATH), help=f"default {RUNS_PATH}")
        a.add_argument("--gates", default=None, help="gates YAML: alarm side, held-out splits")
        a.add_argument("--heldout-split", action="append", default=None, metavar="SPLIT")
        if name == "add":
            a.add_argument("--manifest", default=None, help="B5.14 manifest: refuse held-out ids")
            a.add_argument(
                "--cases", nargs="+", default=None, metavar="SPLIT=PATH", help=CASES_HELP
            )
            a.add_argument("--note", default=None, help="free text, at most 500 characters")
        _common(a)
    i = hs.add_parser("init", help="create an empty run ledger (reading commands need one)")
    i.add_argument("--ledger", default=str(RUNS_PATH), help=f"default {RUNS_PATH}")
    _common(i)
    s = hs.add_parser("show", help="every metric's history, bands and the efficiency spread")
    s.add_argument("--ledger", default=str(RUNS_PATH))
    s.add_argument("--metric", default=None, help="only this metric")
    s.add_argument("--gates", default=None)
    s.add_argument("--heldout-split", action="append", default=None, metavar="SPLIT")
    s.add_argument("--md", default=None, help="write a markdown summary here")
    s.add_argument("--html", default=None, help="write the trend page here")
    _common(s)
    r = hs.add_parser("reset", help="restart a metric's band after a deliberate change")
    r.add_argument("--metric", required=True)
    r.add_argument("--labels", nargs="+", default=None, metavar="KEY=VALUE")
    r.add_argument("--reason", required=True)
    r.add_argument("--ledger", default=str(RUNS_PATH))
    r.add_argument("--project", default=None, help="needed only for an empty ledger")
    _common(r)
    v = hs.add_parser("verify", help="check the hash chain and append-only history of ledgers")
    v.add_argument("files", nargs="*", help="ledgers (default: the three default paths)")
    v.add_argument("--rev", action="append", default=[], metavar="REV")
    v.add_argument("--base", default=None, help="the push base (CI: the push's before)")
    v.add_argument("--default-branch", default=None)
    v.add_argument("--chain-only", action="store_true", help="skip the git append-only check")
    _common(v)
    h.set_defaults(func=cmd_history)

    e = sub.add_parser(
        "experiment",
        help="experiment ledger with a held-out freeze guard (B5.7)",
        epilog=LEDGER_EXIT_CODES,
        formatter_class=fmt,
    )
    es = e.add_subparsers(dest="action", required=True)
    o = es.add_parser("open", help="declare hypothesis, change, primary metric and direction")
    o.add_argument("--hypothesis", required=True)
    o.add_argument("--change", required=True)
    o.add_argument("--metric", required=True)
    o.add_argument("--labels", nargs="+", default=None, metavar="KEY=VALUE")
    o.add_argument("--direction", required=True, choices=("up", "down"))
    o.add_argument("--baseline", required=True, help="the baseline run")
    o.add_argument("--experiments", default=str(EXPERIMENTS_PATH))
    o.add_argument("--manifest", default=None)
    o.add_argument("--cases", nargs="+", default=None, metavar="SPLIT=PATH", help=CASES_HELP)
    _common(o)
    c = es.add_parser("close", help="compare before and after and record the verdict")
    c.add_argument("id")
    c.add_argument("--before", required=True)
    c.add_argument("--after", required=True)
    c.add_argument("--gates", default=None)
    c.add_argument("--manifest", default=None)
    c.add_argument("--cases", nargs="+", default=None, metavar="SPLIT=PATH", help=CASES_HELP)
    c.add_argument("--heldout-split", action="append", default=None, metavar="SPLIT")
    c.add_argument("--heldout", default=None, help="the candidate's held-out result (a run)")
    c.add_argument("--seed", type=int, default=7)
    c.add_argument("--resamples", type=int, default=10_000)
    c.add_argument("--experiments", default=str(EXPERIMENTS_PATH))
    _common(c)
    lk = es.add_parser("look", help="freeze the candidate and reserve its one held-out look")
    lk.add_argument("id")
    lk.add_argument("--candidate", required=True, help="the candidate run or its settings.json")
    lk.add_argument("--manifest", required=True, help="the B5.14 held-out manifest")
    lk.add_argument("--split", default="heldout")
    lk.add_argument("--cases", nargs="+", default=None, metavar="SPLIT=PATH")
    lk.add_argument("--log", required=True, help="the project's B5.14 hash-chained log")
    lk.add_argument("--experiments", default=str(EXPERIMENTS_PATH))
    _common(lk)
    sh = es.add_parser("show", help="experiments and the held-out looks spent per split")
    sh.add_argument("--experiments", default=str(EXPERIMENTS_PATH))
    _common(sh)
    e.set_defaults(func=cmd_experiment)

    d = sub.add_parser(
        "drift",
        help="PSI and calibration drift against a frozen reference (B5.7)",
        epilog=LEDGER_EXIT_CODES,
        formatter_class=fmt,
    )
    ds = d.add_subparsers(dest="action", required=True)
    ref = ds.add_parser("reference", help="freeze a hashed reference profile")
    ref.add_argument("--data", default=None, help="CSV or JSON Lines rows")
    ref.add_argument("--metrics", default=None, help="a metrics-1 file of aggregates")
    ref.add_argument("--features", default=None, help="comma-separated feature columns")
    ref.add_argument("--categorical", default=None, help="features to bin by category")
    ref.add_argument("--prob", default=None, help="predicted probability column")
    ref.add_argument("--label", default=None, help="0/1 outcome column")
    ref.add_argument("--calibration-prefix", default=None, help="e.g. churn (metrics-1 rows)")
    ref.add_argument("--out", required=True)
    ref.add_argument("--manifest", default=None)
    ref.add_argument("--heldout-split", action="append", default=None, metavar="SPLIT")
    ref.add_argument("--seed", type=int, default=7)
    ref.add_argument("--resamples", type=int, default=10_000)
    _common(ref)
    ck = ds.add_parser("check", help="compare a new batch with the reference")
    ck.add_argument("--reference", required=True)
    ck.add_argument("--data", default=None)
    ck.add_argument("--metrics", default=None)
    ck.add_argument("--config", default=None, help="drift config YAML (levels, tolerances)")
    ck.add_argument("--out", default=None, help="write drift-1 JSON here")
    ck.add_argument("--emit-metrics", default=None, help="write a metrics-1 file here")
    ck.add_argument("--project", default=None)
    ck.add_argument("--run-id", default=None)
    ck.add_argument("--prefix", default="drift", help="metric name prefix (default drift)")
    ck.add_argument("--manifest", default=None)
    ck.add_argument("--heldout-split", action="append", default=None, metavar="SPLIT")
    ck.add_argument("--seed", type=int, default=7)
    ck.add_argument("--resamples", type=int, default=10_000)
    _common(ck)
    d.set_defaults(func=cmd_drift)

    sp = sub.add_parser(
        "spend",
        help="spend ledger with a cap alarm (B5.7)",
        epilog=LEDGER_EXIT_CODES,
        formatter_class=fmt,
    )
    ss = sp.add_subparsers(dest="action", required=True)
    ad = ss.add_parser("add", help="append a chained spend line")
    ad.add_argument("--kind", required=True, help="what was spent on (smoke, rejudge, ...)")
    ad.add_argument("--usd", required=True, type=float)
    ad.add_argument("--project", default=None)
    ad.add_argument("--run-id", default=None)
    ad.add_argument("--model", default=None)
    ad.add_argument("--n-cases", type=int, default=None)
    ad.add_argument("--note", default=None)
    ad.add_argument("--spend-ledger", default=str(SPEND_PATH))
    _common(ad)
    sc = ss.add_parser("check", help="window spend against a cap", epilog=LEDGER_EXIT_CODES)
    sc.add_argument("--cap", required=True, type=float, help="USD")
    sc.add_argument("--window", default="month", choices=("total", "day", "month"))
    sc.add_argument("--alarm-fraction", type=float, default=0.8)
    sc.add_argument("--add-estimate", type=float, default=0.0, help="USD about to be spent")
    sc.add_argument("--map", nargs="+", default=None, metavar="FIELD=SOURCE")
    sc.add_argument("--spend-ledger", default=str(SPEND_PATH))
    _common(sc)
    sp.set_defaults(func=cmd_spend)
