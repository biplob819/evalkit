"""`python -m evalkit.security check|run|scripted|report|metrics` (B5.11).

Exit codes: 0 done, 1 a scripted case was not stopped as expected, 2 an input error. Output
never holds held-out case ids or text: held-out splits are printed as counts and aggregates.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evalkit import heldout as H

from .cases import AttackSuite, case_hash, case_key, load_suite
from .errors import SecurityError
from .layers import DEFAULT_ERROR_PREFIXES, LayerSet, load_layers
from .scripted import SCRIPTED_FILE, read_scripted, run_scripted
from .summary import heldout_category_map, summarise, to_metrics, write_outputs
from .trials import DEFAULT_K, TRIALS_FILE, read_trials, run_attacks

RUN_FILE = "run.json"
HC_HELP = (
    "opt-in (D-72): held-out category rows, shown only when every category cell has at "
    "least 5 cases"
)
RUN_SCHEMA = "evalkit.security.run/1"
_PINNED = ("k", "seed", "layers", "allowed_tools", "suite_sha256")


def _layers(text: str | None) -> LayerSet:
    """A comma-separated list of blocking layers, or a YAML/JSON layer-set file."""
    if not text:
        return LayerSet()
    if Path(text).suffix in (".yaml", ".yml", ".json"):
        return load_layers(text)
    blocking = tuple(x.strip() for x in text.split(",") if x.strip())
    prefixes = tuple((p, x) for p, x in DEFAULT_ERROR_PREFIXES if x in blocking)
    return LayerSet(blocking=blocking, error_prefixes=prefixes)


def _suite(args: argparse.Namespace, ls: LayerSet) -> AttackSuite:
    return load_suite(args.cases, heldout=args.heldout, manifest=args.manifest, layers=ls)


def _factory(spec: str, method: str) -> Any:
    """Import MODULE:FACTORY, call it, and check the result has `method`. Every failure is an
    input error (exit 2) with a one-line message."""
    module, _, name = spec.partition(":")
    if not module or not name:
        raise SecurityError(f"{spec!r}: expected MODULE:FACTORY")
    try:
        fn = getattr(importlib.import_module(module), name)
    except Exception as exc:
        raise SecurityError(f"cannot load {spec!r}: {type(exc).__name__}: {exc}") from None
    try:
        obj = fn()
    except Exception as exc:
        raise SecurityError(f"{spec!r} raised {type(exc).__name__}: {exc}") from None
    if not callable(getattr(obj, method, None)):
        raise SecurityError(f"{spec!r} returned {type(obj).__name__}, which has no {method}()")
    return obj


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_settings(out: Path) -> dict[str, Any]:
    path = out / RUN_FILE
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SecurityError(f"{out.name}/{RUN_FILE} is not a JSON object")
    return data


def _settings(out: Path, want: dict[str, Any]) -> dict[str, Any]:
    """Check a resume against run.json (same k, seed, layers, tools and suite); the merged
    settings keep the first run's created_at, names and project. Nothing is written here."""
    have = _read_settings(out)
    for key in _PINNED:
        if key in want and key in have and have[key] != want[key]:
            what = "the attack suite" if key == "suite_sha256" else key
            raise SecurityError(f"{out.name}: {what} differs from the run being resumed")
    return {**want, **{k: v for k, v in have.items() if v is not None}}


def _write_settings(out: Path, settings: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / RUN_FILE).write_text(
        json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _defaults(args: argparse.Namespace, ls: LayerSet, suite: AttackSuite) -> dict[str, Any]:
    return {
        "schema": RUN_SCHEMA,
        "layers": ls.to_dict(),
        "suite_sha256": suite.fingerprint(),
        "cases": {case_key(c): case_hash(c) for c in suite.cases},
        "system": getattr(args, "system_name", None) or "system",
        "project": args.project or "evalkit-security",
        "run_id": args.run_id or Path(args.out).name,
        "created_at": _now(),
    }


def _settings_layers(settings: Mapping[str, Any]) -> LayerSet:
    raw = settings.get("layers")
    if isinstance(raw, Mapping):
        return LayerSet.from_dict(raw)
    if isinstance(raw, list):  # run folders written before the layer set was recorded whole
        return _layers(",".join(str(x) for x in raw))
    return LayerSet()


def _summary_of(out: Path, categories: Mapping[str, str] | None = None) -> dict[str, Any]:
    settings = _read_settings(out)
    trials_path, scripted_path = out / TRIALS_FILE, out / SCRIPTED_FILE
    if not trials_path.is_file() and not scripted_path.is_file():
        raise SecurityError(f"{out.name}: no {TRIALS_FILE} or {SCRIPTED_FILE} to report")
    trials = read_trials(trials_path) if trials_path.is_file() else []
    scripted = read_scripted(scripted_path) if scripted_path.is_file() else []
    cases = settings.get("cases")
    if isinstance(cases, Mapping):  # only the run's own suite counts
        trials = [t for t in trials if cases.get(t.case) == t.case_sha256]
        scripted = [r for r in scripted if r.case in cases]
    return summarise(
        trials,
        k=int(settings.get("k", DEFAULT_K)),
        layers=_settings_layers(settings),
        system=str(settings.get("system", "system")),
        scripted=scripted,
        seed=int(settings.get("seed", 0)),
        heldout_categories=categories,
    )


def _report(out: Path, categories: Mapping[str, str] | None = None) -> int:
    settings = _read_settings(out)
    summary = _summary_of(out, categories)
    write_outputs(
        out,
        summary,
        heldout_categories=categories is not None,
        project=str(settings.get("project", "evalkit-security")),
        run_id=str(settings.get("run_id", out.name)),
        created_at=str(settings.get("created_at", _now())),
    )
    for split, b in summary["splits"].items():
        pk = b["pass_k"]["text"] if b["pass_k"] else "n/a"
        sr = b["trial_success_rate"]["text"] if b["trial_success_rate"] else "n/a"
        print(f"{split}: {b['cases']} case(s); pass^k {pk}; trial success rate {sr}")
    print(f"wrote {out / 'summary.json'}, {out / 'metrics.json'}, {out / 'report.md'}")
    return 0


def _check(args: argparse.Namespace) -> int:
    suite = _suite(args, _layers(args.layers))
    by_cat = Counter(c.owasp for c in suite.public)
    cats = ", ".join(f"{k} {v}" for k, v in sorted(by_cat.items()))
    print(f"public: {len(suite.public)} case(s) ({cats})")
    if args.heldout:
        print(f"heldout: {len(suite.heldout)} case(s); manifest verified")
    return 0


def _run(args: argparse.Namespace) -> int:
    if args.k < 1:
        raise SecurityError("--k must be at least 1")
    if args.log is not None and args.rubric_hash is None:
        raise SecurityError("--log needs --rubric-hash")
    ls = _layers(args.layers)
    suite = _suite(args, ls)
    allowed = [x.strip() for x in args.allowed_tools.split(",") if x.strip()]
    if not allowed:
        raise SecurityError("--allowed-tools names no tool")
    out = Path(args.out)
    want = _defaults(args, ls, suite) | {"k": args.k, "seed": args.seed, "allowed_tools": allowed}
    settings = _settings(out, want)
    system = _factory(args.system, "attack")
    _write_settings(out, settings)
    run_attacks(
        suite,
        system,
        allowed_tools=allowed,
        k=args.k,
        seed=args.seed,
        layers=ls,
        run_dir=out,
        heldout_log=args.log,
        rubric_hash=args.rubric_hash,
    )
    return _report(out, heldout_category_map(suite) if args.heldout_categories else None)


def _scripted(args: argparse.Namespace) -> int:
    ls = _layers(args.layers)
    suite = _suite(args, ls)
    out = Path(args.out)
    settings = _settings(out, _defaults(args, ls, suite))
    surface = _factory(args.surface, "call")
    _write_settings(out, settings)
    results = run_scripted(suite, surface, layers=ls, run_dir=out)
    _report(out)
    failed = [r for r in results if not r.skipped and not r.passed]
    for split in sorted({r.split for r in failed}):
        n = sum(r.split == split for r in failed)
        print(f"{split}: {n} case(s) not stopped as expected")
    return 1 if failed else 0


def _report_cmd(args: argparse.Namespace) -> int:
    categories = None
    if args.heldout_categories:
        if not (args.cases and args.heldout and args.manifest):
            raise SecurityError("--heldout-categories needs --cases, --heldout and --manifest")
        suite = _suite(args, _settings_layers(_read_settings(Path(args.out))))
        settings = _read_settings(Path(args.out))
        if settings.get("suite_sha256") not in (None, suite.fingerprint()):
            raise SecurityError(f"{Path(args.out).name}: the attack suite differs from the run's")
        categories = heldout_category_map(suite)
    return _report(Path(args.out), categories)


def _metrics(args: argparse.Namespace) -> int:
    summaries = [_summary_of(Path(d)) for d in args.dirs]
    m = to_metrics(
        summaries,
        project=args.project or "evalkit-security",
        run_id=args.run_id or Path(args.out).stem,
        created_at=_now(),
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    data = m.model_dump(mode="json", exclude_none=True)
    out.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}: {len(m.metrics)} metric(s) for {len(summaries)} system(s)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m evalkit.security",
        description="Agent security eval kit: attack cases, k trials and pass^k, stopping "
        "layers, a model-free scripted attacker; held-out cases only as split aggregates.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def cases(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--cases", required=True, help="public attack cases (file or folder)")
        sp.add_argument("--heldout", help="held-out attack cases (file or folder)")
        sp.add_argument("--manifest", help="the B5.14 manifest pinning the held-out cases")
        sp.add_argument(
            "--layers",
            help="blocking layers in precedence order, comma-separated, or a YAML/JSON "
            "layer-set file (names, order, error prefixes, roles)",
        )

    def named(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--out", required=True, help="run folder")
        sp.add_argument("--project", help="metrics-1 project name")
        sp.add_argument("--run-id", help="metrics-1 run id (default: the folder name)")

    sp = sub.add_parser("check", help="validate the cases and the held-out manifest")
    cases(sp)
    sp.set_defaults(func=_check)
    sp = sub.add_parser("run", help="run every case k times against a system")
    cases(sp)
    named(sp)
    sp.add_argument("--system", required=True, help="MODULE:FACTORY returning an AttackSystem")
    sp.add_argument("--allowed-tools", required=True, help="comma-separated tool names")
    sp.add_argument("--k", type=int, default=DEFAULT_K)
    sp.add_argument("--seed", type=int, default=0)
    sp.add_argument("--system-name", help="the model label in metrics (default: system)")
    sp.add_argument("--log", help="B5.14 log: score each held-out case once per rubric")
    sp.add_argument("--rubric-hash", help="the rubric hash held-out scoring is logged under")
    sp.add_argument("--heldout-categories", action="store_true", help=HC_HELP)
    sp.set_defaults(func=_run)
    sp = sub.add_parser("scripted", help="send each payload to the tools with no model")
    cases(sp)
    named(sp)
    sp.add_argument("--surface", required=True, help="MODULE:FACTORY returning a ToolSurface")
    sp.set_defaults(func=_scripted)
    sp = sub.add_parser("report", help="re-summarise a run folder")
    sp.add_argument("out", help="run folder")
    sp.add_argument("--heldout-categories", action="store_true", help=HC_HELP)
    sp.add_argument("--cases", help="public attack cases (needed with --heldout-categories)")
    sp.add_argument("--heldout", help="held-out attack cases (needed with --heldout-categories)")
    sp.add_argument("--manifest", help="the B5.14 manifest (needed with --heldout-categories)")
    sp.add_argument("--layers", help=argparse.SUPPRESS)
    sp.set_defaults(func=_report_cmd)
    sp = sub.add_parser("metrics", help="one metrics-1 file from several run folders")
    sp.add_argument("dirs", nargs="+", help="run folders, one system each")
    sp.add_argument("--out", required=True, help="the metrics-1 file to write")
    sp.add_argument("--project", help="metrics-1 project name")
    sp.add_argument("--run-id", help="metrics-1 run id (default: the file name)")
    sp.set_defaults(func=_metrics)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (SecurityError, H.HeldoutError, OSError, json.JSONDecodeError) as exc:
        message = " ".join(str(exc).split())  # one line
        print(f"error: {message}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
