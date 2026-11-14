"""The A2.1 smoke run (GEN-05): a small real run that measures token counts and cost per model,
so the cost model can use measured averages instead of planning assumptions."""

from __future__ import annotations

import json
import statistics
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evalkit.adapters import ModelAdapter
from evalkit.cost import CostEstimate, Price, TokenAssumptions, estimate_for_config
from evalkit.pipeline.generate import Generated, generate_and_validate
from evalkit.runtime import Executor, Store
from evalkit.schemas import Case, RunConfig, TaskConfig


def measure(generated: Sequence[Generated]) -> dict[str, dict[str, Any]]:
    """Per-model averages over calls that returned (transport failures excluded from averages)."""
    by_model: dict[str, list[Generated]] = {}
    for g in generated:
        by_model.setdefault(g.model, []).append(g)
    stats: dict[str, dict[str, Any]] = {}
    for model, items in sorted(by_model.items()):
        returned = [g.completion for g in items if g.completion.error != "transport"]
        errors = Counter(g.completion.error for g in items if g.completion.error)

        def mean(values: list[float]) -> float:
            return round(statistics.fmean(values), 1) if values else 0.0

        stats[model] = {
            "n": len(items),
            "errors": dict(sorted(errors.items())),
            "avg_tokens_in": mean([c.tokens_in for c in returned]),
            "avg_tokens_out": mean([c.tokens_out for c in returned]),
            "avg_tokens_reasoning": mean([c.tokens_reasoning for c in returned]),
            "avg_latency_ms": mean([c.latency_ms for c in returned]),
            "total_cost_usd": round(sum(c.cost_usd for c in returned), 6),
        }
    return stats


def write_measurements(path: Path, stats: dict[str, dict[str, Any]], meta: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"meta": meta, "models": stats}, indent=2, sort_keys=True) + "\n")


LEDGER = Path(__file__).resolve().parents[2] / "docs" / "measurements" / "spend-ledger.jsonl"


def record_spend(
    kind: str, spent_usd: float, detail: dict[str, Any], ledger: Path = LEDGER
) -> float:
    """Append one real-API run to the spend ledger and return the running total in USD."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "kind": kind,
        "spent_usd": round(spent_usd, 6),
        **detail,
    }
    with ledger.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, sort_keys=True) + "\n")
    return round(
        sum(json.loads(line)["spent_usd"] for line in ledger.read_text().splitlines() if line), 6
    )


def assumptions_from(path: Path, base: TokenAssumptions | None = None) -> TokenAssumptions:
    """Generation assumptions from a measurements file: means across models that returned."""
    data = json.loads(path.read_text(encoding="utf-8"))["models"]
    rows = [m for m in data.values() if m["n"] > m["errors"].get("transport", 0)]
    if not rows:
        raise ValueError(f"{path}: no model returned any response")
    base = base or TokenAssumptions()
    return base.model_copy(
        update={
            "t_in": round(statistics.fmean(m["avg_tokens_in"] for m in rows)),
            "t_out": round(statistics.fmean(m["avg_tokens_out"] for m in rows)),
            "t_reason": round(statistics.fmean(m["avg_tokens_reasoning"] for m in rows)),
        }
    )


def estimate(prices: dict[str, Price], config: RunConfig, n: int) -> CostEstimate:
    """Generation-only estimate for a smoke run (no extraction or judging yet)."""
    full = estimate_for_config(prices, config, n)
    lines = tuple(line for line in full.lines if line.node == "generate")
    return CostEstimate(lines=lines, total_usd=sum(line.usd for line in lines))


async def run_smoke(
    *,
    config: RunConfig,
    task: TaskConfig,
    cases: Sequence[Case],
    adapters: Mapping[str, ModelAdapter],
    prices: dict[str, Price],
    store_dir: Path,
    out_path: Path,
    max_cost_usd: float,
) -> dict[str, dict[str, Any]]:
    run_id = "smoke-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    executor = Executor(
        Store(store_dir), concurrency=dict(config.concurrency), max_cost_usd=max_cost_usd
    )
    generated = await generate_and_validate(
        executor, cases, task, config, adapters, prices, run_id=run_id
    )
    stats = measure(generated)
    write_measurements(
        out_path,
        stats,
        {
            "run_id": run_id,
            "task": task.name,
            "n_cases": len(cases),
            "seed": config.seed,
            "models": list(config.models),
            "default_effort": config.default_effort,
            "structured_output": config.structured_output,
            "spent_usd": round(executor.spent_usd, 6),
            "cache_hits": executor.cache_hits,
        },
    )
    return stats
