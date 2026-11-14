#!/usr/bin/env python3
"""FTH-03 live check: the faithfulness fixture against the real extractor and judges.

    uv run --all-extras python scripts/faithfulness_check.py          # estimate only
    uv run --all-extras python scripts/faithfulness_check.py --yes    # spend and record

Each fixture item lists how many of its explanation's claims the evidence supports; the
check passes when both judges agree with the fixture on every item. Writes
docs/measurements/faithfulness-<date>.json and appends to the spend ledger.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = ROOT / "tests" / "fixtures" / "faithfulness" / "items.json"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=ROOT / "configs" / "pubmedqa.yaml")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)

    from evalkit.config import load_run_config
    from evalkit.cost import call_cost, load_prices, price_for
    from evalkit.env import load_dotenv, missing_keys
    from evalkit.run import make_adapters, providers_needed
    from evalkit.runtime import Executor, Store
    from evalkit.scorers.faithfulness import FaithfulnessItem, faithfulness
    from evalkit.smoke import record_spend

    load_dotenv(ROOT / ".env")
    config = load_run_config(args.config)
    prices = load_prices()
    raw = json.loads(FIX.read_text(encoding="utf-8"))
    items = [FaithfulnessItem(**{k: v for k, v in r.items() if k != "expected"}) for r in raw]
    n_claims = sum(r["expected"]["supported"] + r["expected"]["unsupported"] for r in raw)
    est = len(items) * call_cost(price_for(prices, config.extractor), tokens_in=600, tokens_out=500)
    est += sum(
        2 * n_claims * call_cost(price_for(prices, j), tokens_in=800, tokens_out=4096)
        for j in config.judges
    )
    print(
        f"faithfulness check: {len(items)} items, ~{n_claims} claims × "
        f"{len(config.judges)} judges, estimate ≤ ${est:.3f}"
    )
    if missing := missing_keys(providers_needed(config) - {"openai_compatible"}):
        print(f"missing API keys: {', '.join(missing)}")
        return 2
    if not args.yes:
        print("dry run; add --yes to call the models")
        return 0
    with tempfile.TemporaryDirectory() as store:
        # The printed estimate is the cap: spend can never pass the figure shown.
        ex = Executor(Store(Path(store)), max_cost_usd=est)
        try:
            results = asyncio.run(
                faithfulness(
                    items,
                    executor=ex,
                    config=config,
                    adapters=make_adapters(config, prices),
                    prices=prices,
                )
            )
        finally:
            if ex.spent_usd:  # record real spend even if the run stops part-way
                record_spend("faithfulness_check", ex.spent_usd, {"items": len(items)})
        spent = ex.spent_usd
    problems = []
    rows = []
    for res, r in zip(results, raw, strict=True):
        exp = r["expected"]
        for judge in config.judges:
            got = sum(c.verdicts.get(judge) == "supported" for c in res.claims)
            rows.append(
                {
                    "id": res.id,
                    "judge": judge,
                    "n_claims": res.n_claims,
                    "supported": got,
                    "expected_supported": exp["supported"],
                    "claims": [c.model_dump() for c in res.claims],
                }
            )
            if res.n_claims != exp["supported"] + exp["unsupported"]:
                problems.append(
                    f"{res.id}: extractor found {res.n_claims} claims, fixture has "
                    f"{exp['supported'] + exp['unsupported']}"
                )
            if got != exp["supported"]:
                problems.append(
                    f"{res.id}/{judge}: {got} supported, fixture expects {exp['supported']}"
                )
    out = ROOT / "docs" / "measurements" / f"faithfulness-{date.today().isoformat()}.json"
    out.write_text(json.dumps({"results": rows, "problems": problems}, indent=2) + "\n")
    from evalkit.smoke import LEDGER

    total = sum(
        json.loads(line)["spent_usd"] for line in LEDGER.read_text().splitlines() if line.strip()
    )
    print("PASS" if not problems else "FAIL")
    for p in sorted(set(problems)):
        print(f"  {p}")
    print(f"this run spent ${spent:.4f}; all real API spend so far: ${total:.4f}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
