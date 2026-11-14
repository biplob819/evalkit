#!/usr/bin/env python3
"""B6 live check: each configured judge must mark the 10 planted claims unsupported (true in
general, absent from the cited sentences) and must mark 5 control claims supported, so a judge
that always answers "unsupported" cannot pass.

Usage:
    uv run --all-extras python scripts/planted_check.py          # estimate only
    uv run --all-extras python scripts/planted_check.py --yes    # spend and record

Live calls (not batch) so the answer comes back in seconds; cost is a few cents. Writes
docs/measurements/planted-<date>.json and appends to the spend ledger.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from smoke_run import ROOT, load_dotenv, missing_keys

CONTROLS = [
    ("Readmissions fell by 12%.", "Thirty-day readmissions fell by 12% after the programme."),
    ("The trial enrolled 400 patients.", "A total of 400 patients were enrolled in the trial."),
    ("Mortality did not differ between arms.", "Mortality was similar in both arms (p=0.61)."),
    ("Median follow-up was two years.", "Median follow-up was 24 months."),
    ("Costs rose in the intervention group.", "Costs increased in the intervention group."),
]


async def check(judge, adapter, effort):
    from evalkit.pipeline import judge as jm
    from evalkit.pipeline.answer import SchemaError
    from evalkit.schemas import ClaimVerdict

    inputs = jm.planted_inputs(judge, effort)
    controls = [
        jm.JudgeInput(
            claim_id=f"control:{i}",
            claim_text=jm.redact(c),
            cited=(1,),
            sentences=(jm.redact(s),),
            judge_id=judge,
            effort=effort,
        )
        for i, (c, s) in enumerate(CONTROLS)
    ]
    rows, spent = [], 0.0
    for inp in [*inputs, *controls]:
        raw = await adapter.complete(jm._request(inp))
        spent += raw.cost_usd
        try:
            v = jm.parse_verdict(raw.raw_text, inp)
            rows.append({"claim_id": inp.claim_id, "verdict": v.verdict, "quote": v.quote})
        except SchemaError as exc:
            rows.append({"claim_id": inp.claim_id, "verdict": None, "error": str(exc)})
    verdicts = [
        ClaimVerdict(
            claim_id=r["claim_id"],
            judge=judge,
            verdict=r["verdict"],
            evidence=(1,) if r["verdict"] == "supported" else (),
            rationale="",
            judge_prompt_hash=jm.JUDGE_PROMPT_HASH,
        )
        for r in rows
        if r["verdict"] and r["claim_id"].startswith("planted:")
    ]
    problems = jm.planted_claim_check(verdicts)
    problems += [
        f"{r['claim_id']} not supported: {r.get('verdict') or r.get('error')}"
        for r in rows
        if r["claim_id"].startswith("control:") and r["verdict"] != "supported"
    ]
    return {"judge": judge, "spent_usd": round(spent, 6), "problems": problems, "rows": rows}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", type=Path, default=ROOT / "configs" / "pubmedqa.yaml")
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)

    from evalkit.adapters import get_adapter
    from evalkit.config import load_run_config
    from evalkit.cost import call_cost, load_prices, price_for
    from evalkit.pipeline.generate import provider_for
    from evalkit.smoke import record_spend

    load_dotenv(ROOT / ".env")
    config = load_run_config(args.config)
    prices = load_prices()
    n = 10 + len(CONTROLS)
    est = sum(
        n * call_cost(price_for(prices, j), tokens_in=500, tokens_out=600) for j in config.judges
    )
    print(f"planted check: {n} claims × {len(config.judges)} judges, estimate ${est:.3f}")
    providers = {provider_for(j, config.endpoints) for j in config.judges}
    if missing := missing_keys(providers):
        print(f"missing API keys: {', '.join(missing)}")
        return 2
    if not args.yes:
        print("dry run; add --yes to call the judges")
        return 0

    async def run_all():
        out = []
        for j in config.judges:
            adapter = get_adapter(provider_for(j, config.endpoints), prices=prices)
            out.append(await check(j, adapter, config.judge_effort))
        return out

    results = asyncio.run(run_all())
    spent = sum(r["spent_usd"] for r in results)
    path = ROOT / "docs" / "measurements" / f"planted-{date.today().isoformat()}.json"
    path.write_text(json.dumps({"judge_effort": config.judge_effort, "results": results}, indent=2))
    total = record_spend("planted_check", spent, {"judges": list(config.judges)})
    failed = False
    for r in results:
        status = "PASS" if not r["problems"] else "FAIL"
        failed |= bool(r["problems"])
        print(f"{r['judge']:<20} {status}  ${r['spent_usd']:.4f}")
        for p in r["problems"]:
            print(f"    {p}")
    print(f"\nthis run spent ${spent:.4f}; all real API spend so far: ${total:.4f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
