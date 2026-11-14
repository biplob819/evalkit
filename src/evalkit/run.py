"""`evalkit run` orchestration (B2.2): the benchmark graph end to end, resumable.

A run folder is self-describing: `run.json` (what was run), `cases.jsonl`, `records.jsonl`,
`summary.json`, `report.html`, `charts.vl.json`, and `calibration/` once the owner labels.
The folder name comes from the config hash, task, seed, n and the sampled case ids, so the
same command always lands in the same folder and resumes from the content-addressed store.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from evalkit.adapters import ModelAdapter
from evalkit.config import ConfigError, load_run_config, load_task_config
from evalkit.cost import CostEstimate, Price, estimate_for_config, load_prices
from evalkit.datasets import DatasetFileError, load_jsonl_cases, load_pqa_l
from evalkit.env import missing_keys
from evalkit.pipeline.aggregate import aggregate, build_records, family_for, write_summary
from evalkit.pipeline.calibrate import usable_agreements
from evalkit.pipeline.extract import extract_all
from evalkit.pipeline.generate import adapter_key, generate_and_validate, provider_for
from evalkit.pipeline.judge import (
    JudgeSelection,
    JudgingResult,
    judge_claims,
    judge_claims_live,
    select_judges,
)
from evalkit.report import write_report
from evalkit.runtime import Executor, Store
from evalkit.runtime.canonical import sha256_hex
from evalkit.sampling import stratified_sample
from evalkit.schemas import Case, Record, RunConfig, RunSummary, TaskConfig
from evalkit.schemas._base import SCHEMA_VERSION, Model, NonBlank, Timestamp
from evalkit.schemas.calibration import CalibrationReport
from evalkit.schemas.config import Threshold
from evalkit.tasks import builtin_task

PUBMEDQA = "qiaojin/PubMedQA"
DEFAULT_STORE = Path(".evalkit") / "store"

ExitCode = Literal[0, 1, 2, 3, 4]
EXIT_PASS, EXIT_THRESHOLD, EXIT_ERROR, EXIT_COST_CAP, EXIT_PENDING = 0, 1, 2, 3, 4


class RunError(Exception):
    """A configuration or input problem: exit 2 before any model call."""


class RunManifest(Model):
    """`run.json`: everything needed to re-aggregate and re-render without the config."""

    schema_version: NonBlank
    run_id: NonBlank
    task: NonBlank
    task_ref: NonBlank
    config_hash: NonBlank
    seed: int
    n: int
    models: tuple[NonBlank, ...]
    judges: tuple[NonBlank, ...]
    judge_mode: Literal["dual", "single"]
    families: dict[str, Literal["anthropic", "openai", "other"]]
    thresholds: tuple[Threshold, ...]
    live_judges: bool
    started_at: Timestamp
    finished_at: Timestamp | None = None
    spent_usd: float = 0.0  # all real spend on this run folder, across resumes (cap is total)


@dataclass(frozen=True)
class Plan:
    config: RunConfig
    task: TaskConfig
    task_ref: str
    cases: tuple[Case, ...]
    n: int
    run_id: str
    run_dir: Path
    store_dir: Path
    config_hash: str


def load_task(task_arg: str) -> tuple[TaskConfig, Path | None]:
    """A built-in task name, or a path to a task YAML (its dataset path is relative to it)."""
    path = Path(task_arg)
    if path.suffix in (".yaml", ".yml"):
        if not path.is_file():
            raise RunError(f"task file not found: {path}")
        try:
            return load_task_config(path), path.resolve().parent
        except (ConfigError, ValueError) as exc:
            raise RunError(f"invalid task {path}: {exc}") from None
    try:
        return builtin_task(task_arg), None
    except ValueError as exc:
        raise RunError(str(exc)) from None


def load_cases(task: TaskConfig, base_dir: Path | None) -> list[Case]:
    if task.dataset == PUBMEDQA:
        return load_pqa_l()
    if task.dataset.endswith(".jsonl"):
        path = Path(task.dataset)
        if not path.is_absolute():
            path = (base_dir or Path.cwd()) / path
        try:
            return load_jsonl_cases(path, task)
        except DatasetFileError as exc:
            raise RunError(str(exc)) from None
    raise RunError(f"unknown dataset {task.dataset!r}: use {PUBMEDQA!r} or a path to a .jsonl file")


def plan_run(
    config_path: Path,
    *,
    task: str = "pubmedqa",
    n: int = 200,
    out: Path = Path("runs"),
    store_dir: Path = DEFAULT_STORE,
) -> Plan:
    if not config_path.is_file():
        raise RunError(f"config file not found: {config_path}")
    try:
        config = load_run_config(config_path)
    except (ConfigError, ValueError) as exc:
        raise RunError(f"invalid config {config_path}: {exc}") from None
    for name, url in config.endpoints.items():
        if not url.startswith(("http://", "https://")):
            raise RunError(f"endpoint for {name!r} must be an http(s) URL, got {url!r}")
    for m in (*config.models, config.extractor, *config.judges):
        try:
            provider_for(m, config.endpoints)
        except ValueError:
            raise RunError(
                f"unknown model {m!r}: add it to prices.yaml or give it an endpoint in the config"
            ) from None
    task_cfg, base_dir = load_task(task)
    cases = load_cases(task_cfg, base_dir)
    if not 0 < n <= len(cases):
        raise RunError(f"--n must be between 1 and {len(cases)} for task {task_cfg.name}, got {n}")
    sample = stratified_sample(cases, n, seed=config.seed)
    # The folder identity ignores knobs that cannot change results (cost cap, concurrency), so
    # raising a cap or a rate limit resumes the same run instead of stranding its labels.
    config_hash = sha256_hex(config.model_copy(update={"max_cost_usd": None, "concurrency": {}}))[
        :16
    ]
    run_id = (
        "r-"
        + sha256_hex(
            {
                "config": config_hash,
                "task": task_cfg,
                "seed": config.seed,
                "n": n,
                "cases": [c.id for c in sample.cases],
            }
        )[:12]
    )
    return Plan(
        config=config,
        task=task_cfg,
        task_ref=task,
        cases=sample.cases,
        n=n,
        run_id=run_id,
        run_dir=out / run_id,
        store_dir=store_dir,
        config_hash=config_hash,
    )


def providers_needed(config: RunConfig) -> set[str]:
    names = (*config.models, config.extractor, *config.judges)
    return {provider_for(m, config.endpoints) for m in names}


def readiness_problems(
    plan: Plan, prices: Mapping[str, Price], *, include_keys: bool = True
) -> list[str]:
    """Everything that would fail after spending money, found before any call (CLI-04).
    `include_keys=False` (dry runs) skips the API-key checks: estimating needs no keys."""
    problems = []
    config = plan.config
    for m in (*config.models, config.extractor, *config.judges):
        if m not in prices and m not in config.endpoints:
            problems.append(
                f"unknown model {m!r}: add it to prices.yaml or give it an endpoint in the config"
            )
    if not include_keys:
        return problems
    # Models and the extractor must all be reachable; judges need at least one (single-judge
    # mode, D-10), which select_judges enforces once keys are known.
    needed = {provider_for(m, config.endpoints) for m in (*config.models, config.extractor)}
    keys = missing_keys(needed - {"openai_compatible"})
    if keys:
        problems.append(
            f"missing API keys: {', '.join(keys)} (set them in the environment or a .env file)"
        )
    if not available_providers(config) & {provider_for(j, config.endpoints) for j in config.judges}:
        judge_keys = missing_keys({provider_for(j, config.endpoints) for j in config.judges})
        problems.append(f"no judge is usable: set {' or '.join(judge_keys)}")
    return problems


def available_providers(config: RunConfig) -> set[str]:
    """Providers whose keys are set (local endpoints need none)."""
    names = (*config.models, config.extractor, *config.judges)
    providers = {provider_for(m, config.endpoints) for m in names}
    missing = set(missing_keys(providers))
    from evalkit.env import KEYS

    return {p for p in providers if KEYS.get(p) not in missing}


def estimate(plan: Plan, prices: dict[str, Price], *, live_judges: bool) -> CostEstimate:
    return estimate_for_config(prices, plan.config, plan.n, batch_judges=not live_judges)


def make_adapters(config: RunConfig, prices: dict[str, Price]) -> dict[str, ModelAdapter]:
    """Real provider adapters for every model, extractor and judge in the config."""
    from evalkit.adapters import get_adapter

    adapters: dict[str, ModelAdapter] = {}
    for m in (*config.models, config.extractor, *config.judges):
        provider = provider_for(m, config.endpoints)
        key = adapter_key(provider, m)
        if key not in adapters:
            kwargs: dict[str, Any] = {"prices": prices}
            if provider == "openai_compatible":
                kwargs["base_url"] = config.endpoints[m]
            adapters[key] = get_adapter(provider, **kwargs)
    return adapters


# ---- run folder I/O ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _jsonl(items: Sequence[Model]) -> str:
    return "".join(i.model_dump_json() + "\n" for i in items)


def read_manifest(run_dir: Path) -> RunManifest:
    path = run_dir / "run.json"
    if not path.is_file():
        raise RunError(f"{run_dir} is not a run folder (no run.json)")
    return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))


def read_records(run_dir: Path) -> list[Record]:
    lines = (run_dir / "records.jsonl").read_text(encoding="utf-8").splitlines()
    return [Record.model_validate_json(x) for x in lines if x.strip()]


def read_cases(run_dir: Path) -> dict[str, Case]:
    lines = (run_dir / "cases.jsonl").read_text(encoding="utf-8").splitlines()
    cases = [Case.model_validate_json(x) for x in lines if x.strip()]
    return {c.id: c for c in cases}


def read_calibration(run_dir: Path) -> CalibrationReport | None:
    path = run_dir / "calibration" / "calibration.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return CalibrationReport.model_validate(data["current"]) if data.get("current") else None


def summarise(
    run_dir: Path,
    manifest: RunManifest,
    records: Sequence[Record],
    judging_status: Literal["pending", "complete"],
) -> RunSummary:
    """Aggregate a run folder's records, using calibration that still certifies this task."""
    calibration = read_calibration(run_dir)
    selection = JudgeSelection(
        judges=manifest.judges, judge_mode=manifest.judge_mode, n_available=len(manifest.judges)
    )
    judging = [JudgingResult(judge=j, status=judging_status) for j in manifest.judges]
    return aggregate(
        records,
        run_id=manifest.run_id,
        task=manifest.task,
        config_hash=manifest.config_hash,
        models=manifest.models,
        thresholds=manifest.thresholds,
        seed=manifest.seed,
        family_of=lambda m: manifest.families[m],
        selection=selection,
        judging=judging,
        agreements=usable_agreements(calibration, manifest.task),
        started_at=manifest.started_at,
        finished_at=manifest.finished_at,
    )


def render(run_dir: Path) -> Path:
    manifest = read_manifest(run_dir)
    summary = RunSummary.model_validate_json((run_dir / "summary.json").read_text("utf-8"))
    return write_report(
        run_dir,
        summary,
        read_records(run_dir),
        read_cases(run_dir),
        thresholds=manifest.thresholds,
        calibration=read_calibration(run_dir),
    )


def exit_code_for(summary: RunSummary) -> ExitCode:
    if summary.judging_status != "complete":
        return EXIT_PENDING
    return EXIT_PASS if all(m.passes_threshold for m in summary.models) else EXIT_THRESHOLD


# ---- execution --------------------------------------------------------------------------------

Progress = Callable[[str, str], None]  # (stage, message)


@dataclass
class Outcome:
    summary: RunSummary
    exit_code: ExitCode
    spent_usd: float
    run_dir: Path


async def execute(
    plan: Plan,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    *,
    available_providers: set[str],
    max_cost_usd: float | None = None,
    wait_s: float = 30 * 60,
    live_judges: bool = False,
    progress: Progress | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Outcome:
    """Run (or resume) the graph. BudgetExceeded propagates after completed work is cached."""
    say = progress or (lambda stage, msg: None)
    config, run_dir = plan.config, plan.run_dir
    cap = max_cost_usd if max_cost_usd is not None else config.max_cost_usd
    if cap is not None and cap < 0:
        raise RunError(f"--max-cost must be 0 or more, got {cap}")
    if wait_s < 0:
        raise RunError(f"--wait must be 0 or more, got {wait_s / 60:g}")
    try:
        selection = select_judges(config, available_providers)
    except ValueError as exc:
        raise RunError(str(exc)) from None
    previous = read_manifest(run_dir) if (run_dir / "run.json").is_file() else None
    if previous is not None and tuple(previous.judges) != tuple(selection.judges):
        raise RunError(
            f"this run folder was judged by {list(previous.judges)} but only "
            f"{list(selection.judges)} can be used now (missing a provider key?); set the keys "
            "again, or use a different --out for a run with fewer judges"
        )
    spent_before = previous.spent_usd if previous else 0.0
    # The cap is a total for the run folder: earlier invocations' spend counts against it.
    remaining = None if cap is None else max(0.0, cap - spent_before)
    ex = Executor(Store(plan.store_dir), max_cost_usd=remaining)
    started_at = previous.started_at if previous else now()
    manifest = RunManifest(
        schema_version=SCHEMA_VERSION,
        run_id=plan.run_id,
        task=plan.task.name,
        task_ref=plan.task_ref,
        config_hash=plan.config_hash,
        seed=config.seed,
        n=plan.n,
        models=config.models,
        judges=selection.judges,
        judge_mode=selection.judge_mode,
        families={m: family_for(m, config.endpoints) for m in config.models},
        thresholds=config.thresholds or plan.task.thresholds,
        live_judges=live_judges or (previous.live_judges if previous else False),
        started_at=started_at,
        finished_at=previous.finished_at if previous else None,
        spent_usd=spent_before,
    )
    _atomic_write(run_dir / "run.json", manifest.model_dump_json(indent=2) + "\n")
    _atomic_write(run_dir / "cases.jsonl", _jsonl(plan.cases))
    try:
        return await _graph(
            plan, manifest, ex, adapters, prices, selection, say, wait_s, live_judges, now
        )
    finally:
        if ex.spent_usd:  # persist new spend even when the run stops at the cap or fails
            latest = read_manifest(run_dir)
            _atomic_write(
                run_dir / "run.json",
                latest.model_copy(
                    update={"spent_usd": spent_before + ex.spent_usd}
                ).model_dump_json(indent=2)
                + "\n",
            )


async def _graph(
    plan: Plan,
    manifest: RunManifest,
    ex: Executor,
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    selection: JudgeSelection,
    say: Progress,
    wait_s: float,
    live_judges: bool,
    now: Callable[[], datetime],
) -> Outcome:
    config, run_dir = plan.config, plan.run_dir
    say("generate", f"{len(config.models)} models × {plan.n} cases")
    generated = await generate_and_validate(
        ex, plan.cases, plan.task, config, adapters, prices, run_id=plan.run_id
    )
    say("generate", f"done · spent ${ex.spent_usd:.4f}")
    claim_sets = await extract_all(ex, generated, config, adapters, prices, run_id=plan.run_id)
    n_claims = sum(len(cs.claims) for cs in claim_sets)
    say("extract", f"{n_claims} claims · spent ${ex.spent_usd:.4f}")
    items = [(g.case, c) for g, cs in zip(generated, claim_sets, strict=True) for c in cs.claims]

    async def judge_one(judge: str) -> JudgingResult:
        if live_judges:
            r = await judge_claims_live(
                ex, items, judge, config, adapters, prices, run_id=plan.run_id
            )
        else:
            r = await judge_claims(
                ex,
                items,
                judge,
                config,
                adapters,
                prices,
                run_id=plan.run_id,
                max_wait_s=wait_s,
                poll_interval_s=min(30.0, max(1.0, wait_s / 10)) if wait_s else 30.0,
            )
        say("judge", f"{judge}: {r.status}, {len(r.verdicts)} verdicts · spent ${ex.spent_usd:.4f}")
        return r

    # Both judges at once: their batches run in parallel at the provider (D-46).
    outcomes = await asyncio.gather(
        *(judge_one(j) for j in selection.judges), return_exceptions=True
    )
    for o in outcomes:
        if isinstance(o, BaseException):
            raise o
    judging: list[JudgingResult] = [o for o in outcomes if isinstance(o, JudgingResult)]

    complete = all(r.status == "complete" for r in judging)
    if complete and manifest.finished_at is None:
        manifest = manifest.model_copy(update={"finished_at": now()})
        _atomic_write(run_dir / "run.json", manifest.model_dump_json(indent=2) + "\n")
    records = build_records(
        generated,
        claim_sets,
        judging,
        run_id=plan.run_id,
        task=plan.task.name,
        seed=config.seed,
        git_sha=os.environ.get("EVALKIT_GIT_SHA", "unknown"),
        prompt_hash=hashlib.sha256(plan.task.prompt_template.encode()).hexdigest()[:16],
        timestamp=manifest.started_at,
    )
    _atomic_write(run_dir / "records.jsonl", _jsonl(records))
    summary = summarise(run_dir, manifest, records, "complete" if complete else "pending")
    write_summary(summary, run_dir / "summary.json")
    render(run_dir)
    return Outcome(summary, exit_code_for(summary), ex.spent_usd, run_dir)


def default_prices() -> dict[str, Price]:
    return load_prices()


# ---- calibration on a run folder (CLI-02) -----------------------------------------------------


def _cal_dir(run_dir: Path) -> Path:
    return run_dir / "calibration"


def _load_sheet(run_dir: Path) -> list[Any]:
    from evalkit.pipeline.calibrate import SheetRow

    path = _cal_dir(run_dir) / "sheet.json"
    if not path.is_file():
        raise RunError(f"no labelling sheet yet: run `evalkit calibrate export {run_dir}` first")
    return [SheetRow.model_validate(x) for x in json.loads(path.read_text("utf-8"))]


def cal_export(run_dir: Path, *, force: bool = False) -> tuple[Path, Path, int]:
    """Write the blind sheet (private JSON) and its CSV. Refuses to overwrite labels' sheet."""
    from evalkit.pipeline.calibrate import build_sheet, export_sample, sheet_csv

    manifest = read_manifest(run_dir)
    if manifest.finished_at is None:
        raise RunError("judging is still pending: finish the run before calibrating")
    sheet_path = _cal_dir(run_dir) / "sheet.json"
    labels = sorted(p.name for p in _cal_dir(run_dir).glob("labels-*.json"))
    if sheet_path.is_file() and labels:
        raise RunError(
            f"{sheet_path} has labels ({', '.join(labels)}) that only fit this sheet; a new "
            "export would make them unusable. Move the calibration folder aside first"
        )
    if sheet_path.is_file() and not force:
        raise RunError(f"{sheet_path} exists (use --force to replace it; no labels yet)")
    records, cases = read_records(run_dir), read_cases(run_dir)
    try:
        sample = export_sample(records, cases, seed=manifest.seed)
    except ValueError as exc:
        raise RunError(str(exc)) from None
    rows = build_sheet(sample, records, cases, seed=manifest.seed)
    _atomic_write(
        sheet_path, json.dumps([r.model_dump(mode="json") for r in rows], indent=1) + "\n"
    )
    csv_path = _cal_dir(run_dir) / "sheet.csv"
    _atomic_write(csv_path, sheet_csv(rows))
    return sheet_path, csv_path, len(rows)


def cal_labeller(run_dir: Path, *, relabel: bool = False, port: int = 0) -> Any:
    """The D-40 labelling page for this run (first round or the blind relabel)."""
    from evalkit.pipeline.labeller import serve_labeller

    rows = _load_sheet(run_dir)
    name = "labels-relabel.json" if relabel else "labels-first.json"
    try:
        return serve_labeller(
            rows,
            _cal_dir(run_dir) / name,
            round_="relabel" if relabel else "first",
            seed=read_manifest(run_dir).seed,
            port=port,
        )
    except ValueError as exc:
        raise RunError(str(exc)) from None


def cal_import(run_dir: Path, csv_path: Path) -> int:
    """First-round labels from a filled CSV; refuses to overwrite page labels."""
    from evalkit.pipeline.calibrate import labels_from_csv
    from evalkit.pipeline.labeller import load_labels, save_labels

    rows = _load_sheet(run_dir)
    try:
        labels = labels_from_csv(csv_path.read_text(encoding="utf-8"), rows)
    except (OSError, ValueError) as exc:
        raise RunError(f"cannot import {csv_path}: {exc}") from None
    target = _cal_dir(run_dir) / "labels-first.json"
    existing = load_labels(target)
    if existing is not None and existing.labels:
        raise RunError(f"{target} already holds labels; move it aside to import a CSV instead")
    save_labels(target, labels)
    return len(labels.labels)


def cal_score(run_dir: Path, *, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> Any:
    """Score the held-out split under the current rubric, keep the history, re-aggregate."""
    from evalkit.pipeline.calibrate import calibration_report, import_labels
    from evalkit.pipeline.labeller import load_labels

    manifest = read_manifest(run_dir)
    rows = _load_sheet(run_dir)
    first = load_labels(_cal_dir(run_dir) / "labels-first.json")
    if first is None:
        raise RunError("no first-round labels: label with `evalkit calibrate label` first")
    relabel = load_labels(_cal_dir(run_dir) / "labels-relabel.json")
    records = read_records(run_dir)
    verdicts: dict[str, dict[str, Any]] = {j: {} for j in manifest.judges}
    for r in records:
        for v in r.verdicts:
            if v.judge in verdicts:
                verdicts[v.judge][v.claim_id] = v
    hist_path = _cal_dir(run_dir) / "calibration.json"
    history = json.loads(hist_path.read_text("utf-8")) if hist_path.is_file() else {}
    previous = [CalibrationReport.model_validate(x) for x in history.get("history", [])]
    try:
        labels = import_labels(rows, first, relabel)
        report = calibration_report(
            labels,
            verdicts,
            task=manifest.task,
            family_of=lambda m: manifest.families.get(m, "other"),
            seed=manifest.seed,
            scored_at=now(),
            previous=previous,
        )
    except ValueError as exc:
        raise RunError(str(exc)) from None
    entry = report.model_dump(mode="json")
    _atomic_write(
        hist_path,
        json.dumps({"current": entry, "history": [*history.get("history", []), entry]}, indent=1)
        + "\n",
    )
    summary = summarise(run_dir, manifest, records, "complete")
    write_summary(summary, run_dir / "summary.json")
    render(run_dir)
    return report
