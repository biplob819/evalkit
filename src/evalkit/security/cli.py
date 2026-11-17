"""`python -m evalkit.security check|run|scripted|report` (B5.11).

Exit codes: 0 done, 1 a scripted case was not stopped as expected, 2 an input error. Output
never holds held-out case ids or text: held-out splits are printed as counts and aggregates.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evalkit import heldout as H

from .cases import AttackSuite, load_suite
from .errors import SecurityError
from .layers import DEFAULT_ERROR_PREFIXES, LayerSet
from .scripted import SCRIPTED_FILE, read_scripted, run_scripted
from .summary import summarise, write_outputs
from .trials import DEFAULT_K, TRIALS_FILE, read_trials, run_attacks

RUN_FILE = "run.json"
RUN_SCHEMA = "evalkit.security.run/1"


def _layers(text: str | None) -> LayerSet:
    if not text:
        return LayerSet()
    blocking = tuple(x.strip() for x in text.split(",") if x.strip())
    prefixes = tuple((p, x) for p, x in DEFAULT_ERROR_PREFIXES if x in blocking)
    return LayerSet(blocking=blocking, error_prefixes=prefixes)


def _suite(args: argparse.Namespace, ls: LayerSet) -> AttackSuite:
    return load_suite(args.cases, heldout=args.heldout, manifest=args.manifest, layers=ls)


def _factory(spec: str) -> Any:
    module, _, name = spec.partition(":")
    if not module or not name:
        raise SecurityError(f"{spec!r}: expected MODULE:FACTORY")
    try:
        fn = getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as exc:
        raise SecurityError(f"cannot load {spec!r}: {type(exc).__name__}: {exc}") from None
    return fn()


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run_settings(out: Path, want: dict[str, Any]) -> dict[str, Any]:
    """Write or check run.json: a resumed run keeps its settings and its created_at."""
    path = out / RUN_FILE
    if path.is_file():
        have = json.loads(path.read_text(encoding="utf-8"))
        for key in ("k", "seed", "layers", "allowed_tools"):
            if key in want and key in have and have[key] != want[key]:
                raise SecurityError(f"{out.name}: {key} differs from the run being resumed")
        merged = {**want, **{k: v for k, v in have.items() if v is not None}}
    else:
        merged = want
    out.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return merged


def _defaults(args: argparse.Namespace, ls: LayerSet) -> dict[str, Any]:
    return {
        "schema": RUN_SCHEMA,
        "layers": list(ls.blocking),
        "system": getattr(args, "system_name", None) or "system",
        "project": args.project or "evalkit-security",
        "run_id": args.run_id or Path(args.out).name,
        "created_at": _now(),
    }


def _report(out: Path) -> int:
    settings_path = out / RUN_FILE
    settings = (
        json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.is_file() else {}
    )
    trials_path, scripted_path = out / TRIALS_FILE, out / SCRIPTED_FILE
    if not trials_path.is_file() and not scripted_path.is_file():
        raise SecurityError(f"{out.name}: no {TRIALS_FILE} or {SCRIPTED_FILE} to report")
    trials = read_trials(trials_path) if trials_path.is_file() else []
    scripted = read_scripted(scripted_path) if scripted_path.is_file() else []
    blocking = settings.get("layers")
    ls = _layers(",".join(blocking)) if blocking else LayerSet()
    summary = summarise(
        trials,
        k=int(settings.get("k", DEFAULT_K)),
        layers=ls,
        system=str(settings.get("system", "system")),
        scripted=scripted,
    )
    write_outputs(
        out,
        summary,
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
    ls = _layers(args.layers)
    suite = _suite(args, ls)
    allowed = [x.strip() for x in args.allowed_tools.split(",") if x.strip()]
    out = Path(args.out)
    want = _defaults(args, ls) | {"k": args.k, "seed": args.seed, "allowed_tools": allowed}
    _run_settings(out, want)
    system = _factory(args.system)
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
    return _report(out)


def _scripted(args: argparse.Namespace) -> int:
    ls = _layers(args.layers)
    suite = _suite(args, ls)
    out = Path(args.out)
    _run_settings(out, _defaults(args, ls))
    results = run_scripted(suite, _factory(args.surface), layers=ls, run_dir=out)
    _report(out)
    failed = [r for r in results if not r.skipped and not r.passed]
    for split in sorted({r.split for r in failed}):
        n = sum(r.split == split for r in failed)
        print(f"{split}: {n} case(s) not stopped as expected")
    return 1 if failed else 0


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
        sp.add_argument("--layers", help="blocking layers in precedence order, comma-separated")

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
    sp.set_defaults(func=_run)
    sp = sub.add_parser("scripted", help="send each payload to the tools with no model")
    cases(sp)
    named(sp)
    sp.add_argument("--surface", required=True, help="MODULE:FACTORY returning a ToolSurface")
    sp.set_defaults(func=_scripted)
    sp = sub.add_parser("report", help="re-summarise a run folder")
    sp.add_argument("out", help="run folder")
    sp.set_defaults(func=lambda a: _report(Path(a.out)))
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (SecurityError, H.HeldoutError, OSError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
