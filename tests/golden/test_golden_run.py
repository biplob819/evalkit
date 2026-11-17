"""Golden run (A2.5): the benchmark graph end to end on the 12-case PubMedQA fixture with
scripted models, extractor and judges. No network, no spend. The summary must match the
checked-in golden file byte for byte, and a rerun must be served entirely from the cache.

Regenerate the golden file after an intended change with EVALKIT_UPDATE_GOLDEN=1.
"""

import asyncio
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from evalkit.cost import load_prices
from evalkit.datasets.pubmedqa import parse_pqa_l
from evalkit.pipeline.aggregate import (
    aggregate,
    build_records,
    family_for,
    summary_json,
    write_summary,
)
from evalkit.pipeline.extract import extract_all
from evalkit.pipeline.generate import generate_and_validate
from evalkit.pipeline.judge import judge_claims, select_judges
from evalkit.run import eval_overhead
from evalkit.runtime import Executor, Store
from evalkit.schemas import RunConfig, RunSummary
from evalkit.tasks import builtin_task
from tests.pipeline.fakes import ScriptedBatchAdapter

ROOT = Path(__file__).resolve().parents[2]
MINI = ROOT / "tests" / "fixtures" / "pubmedqa_mini.json"
GOLDEN = ROOT / "tests" / "fixtures" / "golden" / "summary_pubmedqa_mini.json"
T0 = datetime(2026, 11, 14, 12, tzinfo=UTC)
T1 = datetime(2026, 11, 14, 13, tzinfo=UTC)
CFG = RunConfig(
    models=("claude-sonnet-5-5", "gpt-6.1-sol", "gpt-6-luna"),
    judges=("claude-opus-5-5", "gpt-6.1-sol"),
    extractor="claude-haiku-5-5",
    seed=7,
)


def _h(*parts: str) -> int:
    return int(hashlib.sha256("|".join(parts).encode()).hexdigest()[:8], 16)


EXTRACT_RE = re.compile(r"Answer \(citations[^:]*: (.*)\nCited sentence numbers: \[(.*)\]")


def reply(req):
    """Deterministic stand-ins for every model role, keyed on the request."""
    if req.model == "extractor":
        m = EXTRACT_RE.search(req.prompt)
        assert m is not None
        text, cited = m.group(1), [int(x) for x in m.group(2).split(",") if x.strip()]
        claim = {"text": text, "cited": cited, "addresses_question": _h(text) % 4 != 0}
        return json.dumps({"claims": [claim]})
    if req.model == "judge":
        unsupported = _h(req.model_id, req.prompt) % 5 == 0
        shown = req.prompt.split("Cited source sentences:")[1]
        first = re.search(r"\[(\d+)\]", shown)
        assert first is not None
        verdict = {
            "verdict": "unsupported" if unsupported else "supported",
            "evidence": [] if unsupported else [int(first.group(1))],
            "quote": None,
            "rationale": "scripted",
        }
        return json.dumps(verdict)
    # generation
    key = _h(req.model, req.prompt)
    if key % 11 == 0:
        return "I cannot produce JSON today."  # a schema failure: counted, never dropped
    sentences = re.findall(r"^\[(\d+)\] (.+)$", req.prompt, re.M)
    n, first_text = sentences[key % min(2, len(sentences))]
    label = ("yes", "no", "maybe")[key % 3]
    return json.dumps({"label": label, "answer": first_text, "citations": [int(n)]})


async def _run(
    store_dir: Path, adapter: ScriptedBatchAdapter, agreements=()
) -> tuple[RunSummary, list, list, dict]:
    cases = parse_pqa_l(json.loads(MINI.read_text(encoding="utf-8")))
    task = builtin_task("pubmedqa")
    prices = load_prices()
    adapters = {"anthropic": adapter, "openai": adapter}
    ex = Executor(Store(store_dir), max_cost_usd=5)
    generated = await generate_and_validate(ex, cases, task, CFG, adapters, prices, run_id="g")
    claim_sets = await extract_all(ex, generated, CFG, adapters, prices, run_id="g")
    items = [(g.case, c) for g, cs in zip(generated, claim_sets, strict=True) for c in cs.claims]
    selection = select_judges(CFG, {"anthropic", "openai"})
    judging = [
        await judge_claims(ex, items, j, CFG, adapters, prices, run_id="g", max_wait_s=1)
        for j in selection.judges
    ]
    records = build_records(
        generated,
        claim_sets,
        judging,
        run_id="g",
        task=task.name,
        seed=CFG.seed,
        git_sha="golden",
        prompt_hash=hashlib.sha256(task.prompt_template.encode()).hexdigest()[:16],
        timestamp=T0,
    )
    summary = aggregate(
        records,
        run_id="g",
        task=task.name,
        config_hash="golden",
        models=CFG.models,
        thresholds=task.thresholds,
        seed=CFG.seed,
        family_of=lambda m: family_for(m, CFG.endpoints),
        selection=selection,
        judging=judging,
        started_at=T0,
        finished_at=T1,
        n_resamples=10_000,  # the published default
        agreements=agreements,
        # the efficiency block's two figures records lack (EFF-02, D-70), as `evalkit run`
        # passes them: price-table dates and the extractor and judge cost per model
        price_dates={m: p.checked_on.isoformat() for m, p in prices.items()},
        eval_overhead_usd=eval_overhead(
            ex, generated, claim_sets, items, selection.judges, CFG, citations=False
        ),
    )
    return summary, records, judging, {c.id: c for c in cases}


@pytest.mark.golden
def test_golden_run_matches_and_reruns_from_cache(tmp_path):
    adapter = ScriptedBatchAdapter(reply)
    summary, *_ = asyncio.run(_run(tmp_path / "store", adapter))
    out = tmp_path / "summary.json"
    write_summary(summary, out)
    if os.environ.get("EVALKIT_UPDATE_GOLDEN") == "1":
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(out.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    assert out.read_text(encoding="utf-8") == GOLDEN.read_text(encoding="utf-8")

    # Sanity of the fixture itself: errors counted, both judges, Holm comparisons present.
    assert summary.judge_mode == "dual" and summary.judging_status == "complete"
    assert sum(sum(m.n_errors.values()) for m in summary.models) >= 1
    assert all(m.n == 12 for m in summary.models)
    assert {c.metric for c in summary.comparisons} == {"accuracy", "headline"}

    # Rerun: every call is served from the cache, and the summary is identical.
    calls = len(adapter.requests)
    batches = len(adapter.submitted)
    again, *_ = asyncio.run(_run(tmp_path / "store", adapter))
    assert len(adapter.requests) == calls and len(adapter.submitted) == batches
    assert summary_json(again) == summary_json(summary)


@pytest.mark.golden
def test_golden_calibration_end_to_end(tmp_path):
    """Export -> blind sheet -> labels -> held-out report -> calibrated summary -> publish."""
    from evalkit.pipeline import calibrate as cal

    adapter = ScriptedBatchAdapter(reply)
    summary, records, judging, cases = asyncio.run(_run(tmp_path / "store", adapter))
    assert summary.calibration_status == "uncalibrated"
    with pytest.raises(cal.PublishRefused):
        cal.publish_guard(summary)

    sample = cal.export_sample(records, cases, seed=CFG.seed, n=20, n_relabel=4)
    rows = cal.build_sheet(sample, records, cases, seed=CFG.seed)
    verdicts = {r.judge: r.verdicts for r in judging}
    # Scripted owner: agrees with the first judge except on every fifth claim.
    opus = verdicts["claude-opus-5-5"]
    first = {
        r.key: cal.LabelEntry(
            label=(
                opus[r.claim_id].verdict
                if i % 5
                else ("unsupported" if opus[r.claim_id].verdict == "supported" else "supported")
            )
        )
        for i, r in enumerate(rows)
    }
    labels = cal.import_labels(
        rows, cal.LabelFile(sheet_id=rows[0].sheet_id, round="first", labels=first)
    )
    report = cal.calibration_report(
        labels,
        verdicts,
        task="pubmedqa",
        family_of=lambda m: family_for(m, CFG.endpoints),
        seed=CFG.seed,
        scored_at=T1,
        n_resamples=1000,
    )
    usable = cal.usable_agreements(report, "pubmedqa")
    assert len(usable) == 2
    calibrated, *_ = asyncio.run(_run(tmp_path / "store", adapter, agreements=usable))
    assert calibrated.calibration_status == "calibrated"
    cal.publish_guard(calibrated)  # dual, calibrated, complete


GOLDEN_REPORT = ROOT / "tests" / "fixtures" / "golden" / "report_pubmedqa_mini.html"


@pytest.mark.golden
def test_golden_report_matches(tmp_path):
    """The run report from the golden run, byte for byte (B2.1, D-42)."""
    from evalkit.report import write_report

    summary, records, _judging, cases = asyncio.run(
        _run(tmp_path / "store", ScriptedBatchAdapter(reply))
    )
    path = write_report(
        tmp_path / "report", summary, records, cases, thresholds=builtin_task("pubmedqa").thresholds
    )
    if os.environ.get("EVALKIT_UPDATE_GOLDEN") == "1":
        GOLDEN_REPORT.write_text(path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    assert path.read_text(encoding="utf-8") == GOLDEN_REPORT.read_text(encoding="utf-8")
