"""Command-line interface (B2.2): `evalkit run`, `evalkit calibrate`, `evalkit report`.

stdlib argparse keeps the install light. Exit codes (Tech Spec §4, OWNER D-44): 0 every
threshold passed, 1 a threshold failed, 2 configuration or runtime error, 3 stopped at the
cost cap, 4 judging pending. Every command takes --json for one machine-readable object.
API keys are read from the environment or ./.env and never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Any

from evalkit import __version__

EXIT_ERROR = 2


def _emit(args: argparse.Namespace, payload: dict[str, Any], text: str) -> None:
    if getattr(args, "json", False):
        print(json.dumps(payload, sort_keys=True, default=str))
    else:
        print(text)


EXIT_CODES = """exit codes:
  0  run finished and every threshold passed
  1  run finished and a threshold failed
  2  configuration or runtime error
  3  stopped at the cost cap (completed work is cached; resume with the same command)
  4  judging pending (rerun the same command to collect, at no extra cost); for agent-run,
     trials interrupted by provider failures (rerun the same command to finish them)"""

_JSON_MODE = False


def _mask(text: str) -> str:
    """Remove any provider key value that leaked into a message."""
    import os

    from evalkit.env import KEYS

    for name in KEYS.values():
        value = os.environ.get(name)
        if value and len(value) >= 8:
            text = text.replace(value, f"<{name}>")
    return text


def _err(msg: str) -> int:
    msg = _mask(msg)
    print(f"error: {msg}", file=sys.stderr)
    if _JSON_MODE:  # --json: stdout always carries exactly one object
        print(json.dumps({"exit_code": EXIT_ERROR, "error": msg}, sort_keys=True))
    return EXIT_ERROR


def _describe(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None)
    if status in (401, 403) or type(exc).__name__ in (
        "AuthenticationError",
        "PermissionDeniedError",
    ):
        return (
            f"the provider rejected the API key ({type(exc).__name__}); check the key and its "
            "permissions, then rerun the same command (completed work is cached)"
        )
    return f"{type(exc).__name__}: {exc}"


def _progress(args: argparse.Namespace):
    if getattr(args, "json", False):
        return None
    return lambda stage, msg: print(f"[{stage}] {msg}", file=sys.stderr, flush=True)


def cmd_run(args: argparse.Namespace) -> int:
    from evalkit import run as R
    from evalkit.env import load_dotenv
    from evalkit.runtime import BudgetExceeded

    load_dotenv(Path.cwd() / ".env")
    try:
        plan = R.plan_run(
            Path(args.config),
            task=args.task,
            n=args.n,
            out=Path(args.out),
            store_dir=Path(args.store),
        )
    except R.RunError as exc:
        return _err(str(exc))
    prices = R.default_prices()
    try:
        est = R.estimate(plan, prices, live_judges=args.live_judges)
    except KeyError as exc:  # unknown price surfaces with a clear message below
        est = None
        unknown = str(exc)
    else:
        unknown = ""
    if args.dry_run:
        problems = R.readiness_problems(plan, prices, include_keys=False)
        if problems or est is None:
            return _err("; ".join(problems) or unknown)
        lines = [
            f"{ln.node:<15} {ln.model:<22} {ln.calls:>6} calls  ${ln.usd:.4f}" for ln in est.lines
        ]
        _emit(
            args,
            {"dry_run": True, "run_id": plan.run_id, "n": plan.n, "estimate": est.model_dump()},
            f"dry run for {plan.task.name}, {plan.n} cases → {plan.run_dir}\n"
            + "\n".join(lines)
            + f"\nestimated total ${est.total_usd:.2f} (no API calls made)",
        )
        return 0
    problems = R.readiness_problems(plan, prices)
    if problems:
        return _err("; ".join(problems))
    cap = args.max_cost if args.max_cost is not None else plan.config.max_cost_usd
    if est is not None and cap is not None and est.total_usd > cap:
        print(
            f"note: estimate ${est.total_usd:.2f} exceeds the cap ${cap:.2f}; the run stops at the "
            "cap and can be resumed with a higher --max-cost",
            file=sys.stderr,
        )
    try:
        adapters = R.make_adapters(plan.config, prices)
        outcome = asyncio.run(
            R.execute(
                plan,
                adapters,
                prices,
                available_providers=R.available_providers(plan.config),
                max_cost_usd=args.max_cost,
                wait_s=args.wait * 60,
                live_judges=args.live_judges,
                progress=_progress(args),
            )
        )
    except BudgetExceeded as exc:
        spent = (
            R.read_manifest(plan.run_dir).spent_usd if (plan.run_dir / "run.json").is_file() else 0
        )
        msg = (
            f"stopped at the cost cap ({exc}); ${spent:.4f} spent on this run so far, and the "
            "cap counts all of it. Completed work is cached: resume with the same command and a "
            f"higher --max-cost (run folder {plan.run_dir})"
        )
        _emit(
            args,
            {
                "exit_code": 3,
                "run_id": plan.run_id,
                "run_dir": str(plan.run_dir),
                "spent_usd_total": round(spent, 6),
                "error": msg,
            },
            msg,
        )
        return 3
    except R.RunError as exc:
        return _err(str(exc))
    s = outcome.summary
    models = [
        {
            "model": m.model,
            "passes_threshold": m.passes_threshold,
            "failed_thresholds": list(m.failed_thresholds),
            "headline": {j: iv.point for j, iv in m.headline.items()},
            "accuracy": m.accuracy.point,
        }
        for m in s.models
    ]
    lines = [
        f"{m['model']:<24} {'PASS' if m['passes_threshold'] else 'FAIL':<5} "
        + " ".join(f"{j}={v:.1%}" for j, v in m["headline"].items())
        + (f"  ({'; '.join(m['failed_thresholds'])})" if m["failed_thresholds"] else "")
        for m in models
    ]
    tail = ""
    if outcome.exit_code == 4:
        tail = (
            "judging pending: batches from an earlier run are still open; rerun without "
            "--live-judges to collect them (never paid twice)"
            if args.live_judges
            else "judging pending: rerun the same command to collect the batches (no extra cost)"
        )
    from evalkit.pipeline.aggregate import stamp_lines

    caveats = stamp_lines(s)
    _emit(
        args,
        {
            "exit_code": outcome.exit_code,
            "caveats": caveats,
            "run_id": s.run_id,
            "run_dir": str(outcome.run_dir),
            "spent_usd": round(outcome.spent_usd, 6),
            "judging_status": s.judging_status,
            "judge_mode": s.judge_mode,
            "calibration_status": s.calibration_status,
            "models": models,
        },
        "".join(f"⚠ {c}\n" for c in caveats)
        + "\n".join(lines)
        + f"\nspent ${outcome.spent_usd:.4f} · report {outcome.run_dir / 'report.html'}"
        + (f"\n{tail}" if tail else ""),
    )
    return outcome.exit_code


def cmd_report(args: argparse.Namespace) -> int:
    from evalkit import run as R
    from evalkit.pipeline.aggregate import stamp_lines
    from evalkit.pipeline.calibrate import PublishRefused, publish_guard
    from evalkit.schemas import RunSummary

    if args.paraphrase:
        return _paraphrase_report(args)
    if args.example:
        import shutil

        from evalkit.onboarding import EXAMPLE_DIR

        run_dir = Path(args.out or "evalkit-example")
        if run_dir.exists() and any(run_dir.iterdir()):
            example_id = json.loads((EXAMPLE_DIR / "run.json").read_text("utf-8"))["run_id"]
            existing = run_dir / "run.json"
            ours = (
                existing.is_file()
                and json.loads(existing.read_text("utf-8")).get("run_id") == example_id
            )
            if not ours:
                return _err(
                    f"{run_dir} is not empty and does not hold the example; choose another --out"
                )
        run_dir.mkdir(parents=True, exist_ok=True)
        for f in EXAMPLE_DIR.iterdir():
            if f.suffix in (".json", ".jsonl"):
                shutil.copyfile(f, run_dir / f.name)
    elif args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        return _err("give a run folder, or --example for the bundled example run")
    try:
        path = R.render(run_dir)
    except (R.RunError, FileNotFoundError) as exc:
        return _err(str(exc))
    summary = RunSummary.model_validate_json((run_dir / "summary.json").read_text("utf-8"))
    caveats = stamp_lines(summary)
    if args.publish:
        try:
            publish_guard(summary)
        except PublishRefused as exc:
            return _err(str(exc))
    _emit(
        args,
        {
            "report": str(path),
            "published": bool(args.publish),
            "caveats": caveats,
            "example": bool(args.example),
        },
        "".join(f"⚠ {c}\n" for c in caveats)
        + (
            "example run: scripted stand-in models on 12 fixture cases, not real results\n"
            if args.example
            else ""
        )
        + f"report {path}",
    )
    return 0


def _paraphrase_report(args: argparse.Namespace) -> int:
    """`evalkit report --paraphrase ORIGINAL_RUN PARAPHRASE_RUN` (B5.8, PAR-02)."""
    from evalkit import paraphrase as P

    if args.run_dir or args.example:
        return _err("--paraphrase takes its two run folders; give no other run folder or --example")
    original, para = (Path(p) for p in args.paraphrase)
    try:
        rep = P.contamination_report(original, para)
    except (P.ProbeError, OSError, ValueError) as exc:
        return _err(str(exc))
    data = rep.model_dump(mode="json")
    path = para / "contamination.json"
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if args.publish:
        try:
            P.require_final(rep)
        except P.ProbeError as exc:
            return _err(str(exc))
    _emit(
        args,
        {
            "contamination": data,
            "report": str(path),
            "status": rep.status,
            "spot_check": rep.spot_check,
            "published": bool(args.publish),
        },
        P.report_text(rep) + f"\nreport {path}",
    )
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    from evalkit.onboarding import init_project

    root = Path(args.dir)
    try:
        res = init_project(root, force=args.force)
    except (ValueError, OSError) as exc:
        return _err(f"cannot initialise {root}: {exc}")
    lines = [f"wrote {f}" for f in res.written] + [
        f"kept existing {f} (use --force to replace)" for f in res.skipped
    ]
    cd = f"  cd {root}\n" if str(root) != "." else ""
    nxt = (
        "next:\n" + cd + "  cp .env.example .env    # then add your keys\n"
        "  evalkit doctor\n"
        "  evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3 --dry-run\n"
        "  evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3"
    )
    _emit(
        args,
        {"written": res.written, "skipped": res.skipped},
        "\n".join(lines) + "\n" + nxt,
    )
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from evalkit.env import load_dotenv
    from evalkit.onboarding import doctor

    load_dotenv(Path.cwd() / ".env")
    checks = doctor(offline=args.offline, store=Path(args.store), config=Path(args.config))
    blocking = [c for c in checks if c.blocking and not c.ok]
    _emit(
        args,
        {
            "ready": not blocking,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail, "blocking": c.blocking}
                for c in checks
            ],
        },
        "\n".join(
            f"{'ok' if c.ok else ('FAIL' if c.blocking else 'warn'):<4}  {c.name:<18} {c.detail}"
            for c in checks
        )
        + ("\nready to run" if not blocking else "\nnot ready: fix the FAIL lines above"),
    )
    return 0 if not blocking else EXIT_ERROR


def cmd_calibrate(args: argparse.Namespace) -> int:
    from evalkit import run as R

    if getattr(args, "criterion", None):  # B5.9: a criterion sheet, not groundedness
        from evalkit.integrity import cli_calibrate

        return cli_calibrate(args)
    only = [
        o
        for o in ("manifest", "planted", "context", "log", "rubric_hash")
        if getattr(args, o, None)
    ]
    if only or (args.action == "import" and getattr(args, "relabel", False)):
        return _err(f"--{(only or ['relabel'])[0].replace('_', '-')} needs --criterion")
    run_dir = Path(args.run_dir)
    try:
        if args.action == "export":
            sheet, csv_path, n = R.cal_export(run_dir, force=args.force)
            _emit(
                args,
                {"sheet": str(sheet), "csv": str(csv_path), "claims": n},
                f"{n} claims to label. Label in the browser: evalkit calibrate label {run_dir}\n"
                f"(or fill {csv_path} and run: evalkit calibrate import {run_dir} FILE.csv)",
            )
        elif args.action == "label":
            server = R.cal_labeller(run_dir, relabel=args.relabel, port=args.port)
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
            n = R.cal_import(run_dir, Path(args.csv))
            _emit(args, {"imported": n}, f"imported {n} labels")
        else:
            report = R.cal_score(run_dir)
            kappas = {a.judge: a.kappa.point for a in report.agreements}
            _emit(
                args,
                {"kappa": kappas, "judge_prompt_hash": report.judge_prompt_hash},
                "held-out kappa: "
                + ", ".join(f"{j} {k:.2f}" for j, k in kappas.items())
                + f"\nreport {run_dir / 'report.html'}",
            )
    except (R.RunError, FileNotFoundError) as exc:
        return _err(str(exc))
    return 0


def cmd_agent_run(args: argparse.Namespace) -> int:
    from evalkit.adapters import get_adapter
    from evalkit.agents import runner as A
    from evalkit.agents.simuser import SIMULATORS
    from evalkit.cost import load_prices
    from evalkit.env import load_dotenv, missing_keys
    from evalkit.pipeline.generate import provider_for
    from evalkit.runtime import BudgetExceeded

    load_dotenv(Path.cwd() / ".env")
    prices = load_prices()
    try:
        plan = A.plan_agent_run(
            agents=args.agents or A.AGENTS,
            simulators=args.simulators if args.simulators is not None else SIMULATORS,
            k=args.k,
            seed=args.seed,
            out=Path(args.out),
            store_dir=Path(args.store),
            heldout_dir=None if args.no_heldout else Path(args.heldout),
            heldout_manifest=Path(args.heldout).parent / "MANIFEST.json",
            only=args.tasks or (),
        )
        costs = A.estimate_usd(plan, prices)
    except (A.AgentRunError, KeyError) as exc:
        return _err(str(exc))
    est = A.total(costs)
    n_ambiguous = sum(1 for j in plan.jobs if j.simulator)
    header = (
        f"agent run {plan.run_id}: {len(plan.tasks)} tasks "
        f"({'with' if plan.heldout_present else 'without'} held-out), "
        f"{len(plan.agents)} agents, k={plan.k}, {len(plan.jobs)} trials "
        f"({n_ambiguous} with a simulated user)"
    )
    lines = "\n".join(f"  {m:<22} ${usd:.2f}" for m, usd in sorted(costs.items()))
    if args.dry_run:
        _emit(
            args,
            {
                "dry_run": True,
                "run_id": plan.run_id,
                "trials": len(plan.jobs),
                "tasks": len(plan.tasks),
                "heldout_present": plan.heldout_present,
                "estimate_usd": round(est, 4),
                "by_model": {m: round(v, 4) for m, v in costs.items()},
            },
            f"{header}\n{lines}\nestimated total ${est:.2f} (no API calls made)",
        )
        return 0
    providers = {provider_for(m) for m in (*plan.agents, *plan.simulators)}
    if missing := missing_keys(providers):
        return _err(f"missing API keys: {', '.join(missing)}")
    # The estimate is always shown before the first call (stderr, so --json stays one object).
    print(
        f"{header}\n{lines}\nestimated total ${est:.2f}; cap ${args.max_cost:.2f}",
        file=sys.stderr,
    )
    if est > args.max_cost:
        print(
            f"note: estimate ${est:.2f} exceeds the cap ${args.max_cost:.2f}; the run stops at "
            "the cap and resumes with a higher --max-cost",
            file=sys.stderr,
        )
    adapters = {p: get_adapter(p, prices=prices) for p in providers}
    try:
        outcome = asyncio.run(
            A.execute_agent_run(
                plan, adapters, prices, max_cost_usd=args.max_cost, concurrency=args.concurrency
            )
        )
    except BudgetExceeded as exc:
        m = A.read_agent_manifest(plan.run_dir)
        spent = m.spent_usd if m else 0.0
        msg = (
            f"stopped at the cost cap ({exc}); ${spent:.4f} spent on this run so far. Finished "
            f"trials are cached: resume with the same command and a higher --max-cost "
            f"(run folder {plan.run_dir})"
        )
        _emit(
            args,
            {
                "exit_code": 3,
                "run_id": plan.run_id,
                "spent_usd_total": round(spent, 6),
                "error": msg,
            },
            msg,
        )
        return 3
    s = outcome.summary
    rows = [
        {
            "agent": r.agent,
            "view": r.view,
            "split": r.split,
            "success": round(r.metrics["success"].point, 4),
            # absent when no task has all k trials for every agent (lost trials)
            "pass_hat_k": round(r.metrics["pass_hat_k"].point, 4)
            if "pass_hat_k" in r.metrics
            else None,
        }
        for r in s.rows
    ]
    text = "\n".join(
        f"{r['agent']:<20} {(r['view'] or 'no simulator').split('+')[0]:<18} {r['split']:<8} "
        f"success {r['success']:.0%}  pass^k "
        + (f"{r['pass_hat_k']:.0%}" if r["pass_hat_k"] is not None else "– (trials missing)")
        for r in rows
    )
    seps = [x for x in s.separation if x.separated]
    code = 4 if outcome.incomplete else 0
    resume = (
        f"\n{outcome.incomplete} trial(s) hit provider failures: rerun the same command to "
        "finish them (finished trials replay free)"
        if outcome.incomplete
        else ""
    )
    _emit(
        args,
        {
            "exit_code": code,
            "estimate_usd": round(est, 4),
            "incomplete_trials": outcome.incomplete,
            "run_id": plan.run_id,
            "run_dir": str(outcome.run_dir),
            "spent_usd": round(outcome.spent_usd, 6),
            "rows": rows,
            "separated": [x.model_dump(mode="json") for x in seps],
            "note": s.note,
        },
        f"{text}\n{len(seps)} separated gap(s) · spent ${outcome.spent_usd:.4f} · report "
        f"{outcome.run_dir / 'report.html'}" + (f"\n{s.note}" if s.note else "") + resume,
    )
    return code


def cmd_demo_cache(args: argparse.Namespace) -> int:
    from evalkit.adapters import get_adapter
    from evalkit.config import load_run_config
    from evalkit.cost import load_prices
    from evalkit.demo import page as P
    from evalkit.demo.engine import estimate_request_usd, request_bound_usd
    from evalkit.env import load_dotenv, missing_keys
    from evalkit.pipeline.generate import provider_for

    load_dotenv(Path.cwd() / ".env")
    try:
        config = load_run_config(Path(args.config))
        samples = P.load_samples(Path(args.samples))
    except (OSError, ValueError) as exc:
        return _err(str(exc))
    prices = load_prices()
    out = Path(args.out)
    replay = P.load_replay(out)
    todo = [s for s in samples if not P.is_cached(replay, s)]
    judge = config.judges[0]
    est = sum(estimate_request_usd(config, prices, judge, s.question, s.sentences) for s in todo)
    bounds = [
        request_bound_usd(s.question, s.sentences, config=config, prices=prices) for s in todo
    ]
    head = (
        f"demo cache: {len(samples)} samples, {len(samples) - len(todo)} already cached, "
        f"{len(todo)} to answer; estimate ${est:.2f} (worst case ${sum(bounds):.2f}), "
        f"cap ${args.max_cost:.2f}"
    )
    if args.dry_run or not todo:
        _emit(
            args,
            {
                "dry_run": args.dry_run,
                "to_answer": len(todo),
                "estimate_usd": round(est, 4),
                "worst_case_usd": round(sum(bounds), 4),
            },
            head + ("" if todo else "\nnothing to do"),
        )
        return 0
    print(head, file=sys.stderr)
    providers = {
        provider_for(m, config.endpoints) for m in (*config.models, config.extractor, judge)
    }
    if missing := missing_keys(providers):
        return _err(f"missing API keys: {', '.join(missing)}")
    adapters = {p: get_adapter(p, prices=prices) for p in providers}

    async def answer_all() -> tuple[float, int, int]:
        # One event loop for every sample: the provider clients are bound to the loop that
        # created them, so a fresh asyncio.run per sample breaks them after the first.
        spent, added = 0.0, 0
        for s, bound in zip(todo, bounds, strict=True):
            if spent + bound > args.max_cost:  # the next sample could pass the cap: stop
                return spent, added, len(todo) - added
            part: dict[str, list] = {}
            await P.build_replay(
                [s], part, config=config, adapters=adapters, prices=prices, redo=True
            )
            replay.update(part)
            spent += sum(c.cost_usd for cards in part.values() for c in cards)
            added += 1
            P.save_replay(out, replay)  # after every sample: nothing paid for is lost
        return spent, added, 0

    spent, added, stopped = asyncio.run(answer_all())
    msg = f"answered {added} sample(s), spent ${spent:.4f} → {out}"
    if stopped:
        msg += f"; {stopped} left (raise --max-cost to continue, cached ones are not re-run)"
    _emit(
        args,
        {"answered": added, "left": stopped, "spent_usd": round(spent, 6), "out": str(out)},
        msg,
    )
    return 3 if stopped else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="evalkit",
        description="Evaluate your own task across Claude and OpenAI models.",
        epilog=EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"evalkit {__version__}")
    sub = p.add_subparsers(dest="command")

    r = sub.add_parser(
        "run",
        help="run (or resume) a benchmark",
        epilog=EXIT_CODES,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    r.add_argument("config", help="run config YAML")
    r.add_argument("--task", default="pubmedqa", help="built-in task name or a task YAML path")
    r.add_argument("--n", type=int, default=200, help="number of cases (default 200)")
    r.add_argument("--out", default="runs", help="folder for run folders (default ./runs)")
    r.add_argument(
        "--store", default=".evalkit/store", help="cache folder (default .evalkit/store)"
    )
    r.add_argument(
        "--max-cost",
        type=float,
        default=None,
        help="total USD this run folder may spend, across resumes (default: config max_cost_usd)",
    )
    r.add_argument("--dry-run", action="store_true", help="print the estimate; no API calls")
    r.add_argument(
        "--wait", type=float, default=30, help="minutes to wait for judge batches (default 30)"
    )
    r.add_argument("--live-judges", action="store_true", help="judge now at full price")
    r.add_argument("--json", action="store_true", help="print one JSON object")
    r.set_defaults(func=cmd_run)

    ag = sub.add_parser("agent-run", help="run (or resume) the agent suite")
    ag.add_argument(
        "--agents",
        nargs="+",
        default=None,
        help="agent models (default: claude-sonnet-5-5 gpt-6.1-sol)",
    )
    ag.add_argument(
        "--simulators",
        nargs="*",
        default=None,
        help="simulated-user models (default: claude-haiku-5-5 gpt-6-luna)",
    )
    ag.add_argument("--k", type=int, default=3, help="trials per task (default 3)")
    ag.add_argument("--seed", type=int, default=7)
    ag.add_argument("--tasks", nargs="+", default=None, help="only these task ids")
    ag.add_argument(
        "--heldout",
        default="heldout/agent_tasks",
        help="held-out task folder, checked against its MANIFEST.json",
    )
    ag.add_argument("--no-heldout", action="store_true", help="public tasks only")
    ag.add_argument("--out", default="runs", help="folder for run folders (default ./runs)")
    ag.add_argument("--store", default=".evalkit/store", help="cache folder")
    ag.add_argument(
        "--max-cost",
        type=float,
        default=25.0,
        help="total USD this run folder may spend, across resumes (default 25)",
    )
    ag.add_argument("--concurrency", type=int, default=8, help="trials at once (default 8)")
    ag.add_argument("--dry-run", action="store_true", help="print the estimate; no API calls")
    ag.add_argument("--json", action="store_true", help="print one JSON object")
    ag.set_defaults(func=cmd_agent_run)

    dc = sub.add_parser("demo-cache", help="answer the demo's sample questions for replay mode")
    dc.add_argument("--samples", default="demo/samples.json")
    dc.add_argument("--out", default="demo/replay.json")
    dc.add_argument("--config", default="configs/pubmedqa.yaml")
    dc.add_argument("--max-cost", type=float, default=10.0, help="USD cap for this run")
    dc.add_argument("--dry-run", action="store_true", help="print the estimate; no API calls")
    dc.add_argument("--json", action="store_true")
    dc.set_defaults(func=cmd_demo_cache)

    c = sub.add_parser("calibrate", help="label claims and score the judges")
    csub = c.add_subparsers(dest="action", required=True)
    for name, helptext in (
        ("export", "write the blind labelling sheet"),
        ("label", "open the local labelling page"),
        ("import", "import a filled CSV sheet"),
        ("score", "score the held-out split"),
    ):
        a = csub.add_parser(name, help=helptext)
        a.add_argument("run_dir", help="the run folder (runs/r-...)")
        a.add_argument("--json", action="store_true", help="print one JSON object")
        if name == "export":
            a.add_argument("--force", action="store_true", help="replace an existing sheet")
        if name == "label":
            a.add_argument("--relabel", action="store_true", help="the blind relabel round")
            a.add_argument("--port", type=int, default=0, help="port (default: any free port)")
        if name == "import":
            a.add_argument("csv", help="the filled sheet.csv")
        # B5.9: a calibration sheet for any declared criterion (evalkit.integrity)
        a.add_argument("--criterion", default=None, help="criterion YAML (B5.9 criterion sheet)")
        if name in ("export", "score"):
            a.add_argument("--manifest", default=None, help="B5.14 held-out manifest (--criterion)")
        if name == "export":
            a.add_argument(
                "--planted", default=None, help="planted known-answer items (--criterion)"
            )
        if name == "import":
            a.add_argument("--relabel", action="store_true", help="second-pass CSV (--criterion)")
        if name == "label":
            a.add_argument(
                "--context", action="append", default=None, help="labeller-only context file "
                "template, e.g. solutions/{assessment_id}.md (--criterion; repeatable)"
            )  # fmt: skip
        if name == "score":
            a.add_argument("--log", default=None, help="B5.14 hash-chained log (--criterion)")
            a.add_argument("--rubric-hash", default=None, help="current rubric hash (--criterion)")
    c.set_defaults(func=cmd_calibrate)

    rep = sub.add_parser("report", help="re-render a run's report (or --example)")
    rep.add_argument("run_dir", nargs="?", help="the run folder (runs/r-...)")
    rep.add_argument("--example", action="store_true", help="render the bundled example run")
    rep.add_argument("--out", default=None, help="folder for --example (default ./evalkit-example)")
    rep.add_argument("--publish", action="store_true", help="refuse unless dual and calibrated")
    rep.add_argument(
        "--paraphrase",
        nargs=2,
        metavar=("ORIGINAL_RUN", "PARAPHRASE_RUN"),
        default=None,
        help="contamination report: original vs paraphrased wording of the probe (B5.8)",
    )
    rep.add_argument("--json", action="store_true")
    rep.set_defaults(func=cmd_report)

    ini = sub.add_parser("init", help="scaffold evalkit.yaml, .env.example and a sample task")
    ini.add_argument("dir", nargs="?", default=".", help="project folder (default: here)")
    ini.add_argument("--force", action="store_true", help="replace EvalKit's own files")
    ini.add_argument("--json", action="store_true", help="print one JSON object")
    ini.set_defaults(func=cmd_init)

    doc = sub.add_parser("doctor", help="check Python, SDKs, keys (names only), network, prices")
    doc.add_argument("--offline", action="store_true", help="skip the network checks")
    doc.add_argument("--store", default=".evalkit/store", help="cache folder to measure")
    doc.add_argument(
        "--config", default="evalkit.yaml", help="run config whose providers must be ready"
    )
    doc.add_argument("--json", action="store_true", help="print one JSON object")
    doc.set_defaults(func=cmd_doctor)
    return p


def main(argv: list[str] | None = None) -> int:
    global _JSON_MODE
    # Windows pipes default to cp1252, which cannot print the report's symbols (⚠, ·, –).
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    _JSON_MODE = bool(getattr(args, "json", False))
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print(
            "interrupted: completed work is cached; rerun the same command to resume",
            file=sys.stderr,
        )
        return 130
    except Exception as exc:  # anything unexpected is a runtime error (exit 2), never "1"
        return _err(_describe(exc))


if __name__ == "__main__":
    sys.exit(main())
