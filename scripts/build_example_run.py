#!/usr/bin/env python3
"""Rebuild the bundled example run (`evalkit report --example`, ONB-03) from the golden
fixture pipeline: scripted models on the 12-case PubMedQA fixture, so no keys or network.

    uv run python scripts/build_example_run.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def build(out: Path) -> None:
    from tests.golden.test_golden_run import CFG, T0, T1, _run, reply
    from tests.pipeline.fakes import ScriptedBatchAdapter

    from evalkit.pipeline.aggregate import family_for, write_summary
    from evalkit.run import RunManifest, _atomic_write, _jsonl
    from evalkit.schemas._base import SCHEMA_VERSION
    from evalkit.tasks import builtin_task

    with tempfile.TemporaryDirectory() as store:
        summary, records, _judging, cases = asyncio.run(
            _run(Path(store), ScriptedBatchAdapter(reply))
        )
    task = builtin_task("pubmedqa")
    manifest = RunManifest(
        schema_version=SCHEMA_VERSION,
        run_id=summary.run_id,
        task=summary.task,
        task_ref="pubmedqa",
        config_hash=summary.config_hash,
        seed=CFG.seed,
        n=len(cases),
        models=CFG.models,
        judges=CFG.judges,
        judge_mode=summary.judge_mode,
        families={m: family_for(m, CFG.endpoints) for m in CFG.models},
        thresholds=task.thresholds,
        live_judges=False,
        started_at=T0,
        finished_at=T1,
    )
    out.mkdir(parents=True, exist_ok=True)
    _atomic_write(out / "run.json", manifest.model_dump_json(indent=2) + "\n")
    _atomic_write(out / "cases.jsonl", _jsonl(list(cases.values())))
    _atomic_write(out / "records.jsonl", _jsonl(records))
    write_summary(summary, out / "summary.json")


if __name__ == "__main__":
    from evalkit.onboarding import EXAMPLE_DIR

    build(EXAMPLE_DIR)
    print(f"example run written to {EXAMPLE_DIR}")
