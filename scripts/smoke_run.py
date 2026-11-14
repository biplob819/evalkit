#!/usr/bin/env python3
"""A2.1 smoke run: generate + validate on a few seeded PubMedQA cases with real models.

Usage:
    uv run --all-extras python scripts/smoke_run.py --n 20            # print the estimate only
    uv run --all-extras python scripts/smoke_run.py --n 20 --yes      # spend and measure

Needs ANTHROPIC_API_KEY and OPENAI_API_KEY (environment or a git-ignored .env). Keys are
never printed. Writes docs/measurements/smoke-<date>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
from evalkit.env import KEYS, load_dotenv, missing_keys, parse_dotenv_line  # noqa: E402

__all__ = ["KEYS", "ROOT", "load_dotenv", "missing_keys", "parse_dotenv_line"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--config", type=Path, default=ROOT / "configs" / "pubmedqa.yaml")
    ap.add_argument("--max-cost", type=float, default=1.0)
    ap.add_argument("--yes", action="store_true", help="actually call the models")
    args = ap.parse_args(argv)

    from evalkit.config import load_run_config
    from evalkit.cost import load_prices
    from evalkit.pipeline.generate import provider_for
    from evalkit.smoke import estimate

    load_dotenv(ROOT / ".env")
    config = load_run_config(args.config)
    providers = {provider_for(m, config.endpoints) for m in config.models}
    prices = load_prices()
    est = estimate(prices, config, args.n)
    print(
        f"smoke run: {args.n} cases × {len(config.models)} models, "
        f"generation-only estimate ${est.total_usd:.3f}"
    )
    for line in est.lines:
        print(f"  {line.model:<22} {line.calls:>4} calls  ${line.usd:.4f}")
    if est.total_usd > args.max_cost:
        print(f"estimate exceeds --max-cost ${args.max_cost:.2f}; not running")
        return 3
    missing = missing_keys(providers)
    if missing:
        print(f"missing API keys: {', '.join(missing)} (set them in the environment or .env)")
        return 2
    if not args.yes:
        print("dry run; add --yes to call the models")
        return 0

    from evalkit.adapters import get_adapter
    from evalkit.datasets import load_pqa_l
    from evalkit.sampling import stratified_sample
    from evalkit.smoke import run_smoke
    from evalkit.tasks import builtin_task

    task = builtin_task("pubmedqa")
    cases = stratified_sample(load_pqa_l(), args.n, seed=config.seed).cases
    from evalkit.pipeline.generate import adapter_key

    adapters = {}
    for m in config.models:
        p = provider_for(m, config.endpoints)
        key = adapter_key(p, m)
        if key not in adapters:
            kwargs = {"base_url": config.endpoints[m]} if p == "openai_compatible" else {}
            adapters[key] = get_adapter(p, prices=prices, **kwargs)
    out = ROOT / "docs" / "measurements" / f"smoke-{date.today().isoformat()}.json"
    stats = asyncio.run(
        run_smoke(
            config=config,
            task=task,
            cases=cases,
            adapters=adapters,
            prices=prices,
            store_dir=ROOT / ".evalkit" / "store",
            out_path=out,
            max_cost_usd=args.max_cost,
        )
    )
    print(
        f"\n{'model':<22} {'n':>3} {'errors':<18} {'in':>7} {'out':>6} "
        f"{'reason':>7} {'ms':>7} {'cost':>9}"
    )
    for model, s in stats.items():
        errs = ",".join(f"{k}:{v}" for k, v in s["errors"].items()) or "-"
        print(
            f"{model:<22} {s['n']:>3} {errs:<18} {s['avg_tokens_in']:>7} {s['avg_tokens_out']:>6} "
            f"{s['avg_tokens_reasoning']:>7} {s['avg_latency_ms']:>7} ${s['total_cost_usd']:>8.4f}"
        )
    import json as _json

    from evalkit.smoke import record_spend

    meta = _json.loads(out.read_text())["meta"]
    total = record_spend("smoke", meta["spent_usd"], {"run_id": meta["run_id"], "n_cases": args.n})
    print(f"\nthis run spent ${meta['spent_usd']:.4f}; all real API spend so far: ${total:.4f}")
    print(f"measurements written to {out.relative_to(ROOT)}")
    dead = [m for m, s in stats.items() if s["errors"].get("transport", 0) == s["n"]]
    if dead:
        print(f"every call failed for: {', '.join(dead)}")
        return 4
    return 0


if __name__ == "__main__":
    sys.exit(main())
