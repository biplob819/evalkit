#!/usr/bin/env python3
"""Measure agent-run token use from a finished (pilot) run folder, for the cost estimate.

    uv run --all-extras python scripts/measure_agent_tokens.py runs/a-<id>

Prints per agent: turns per trial, prompt and output tokens per turn, and the effective share
of prompt tokens served from the provider cache (from actual spend against list prices), and
writes docs/measurements/agent-tokens-<date>.json. No API calls.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print(__doc__)
        return 2
    from evalkit.cost import load_prices, price_for
    from evalkit.schemas import Trajectory

    run = Path(args[0])
    lines = (run / "trajectories.jsonl").read_text(encoding="utf-8").splitlines()
    trajs = [Trajectory.model_validate_json(x) for x in lines if x.strip()]
    prices = load_prices()
    out: dict[str, dict[str, float]] = {}
    for agent in sorted({t.agent for t in trajs}):
        mine = [t for t in trajs if t.agent == agent and t.steps]
        steps = [s for t in mine for s in t.steps]
        if not steps:
            continue
        p = price_for(prices, agent)
        t_in = math.fsum(s.tokens_in for s in steps)
        t_out = math.fsum(s.tokens_out for s in steps)  # visible + reasoning
        spent = math.fsum(t.cost_usd for t in mine)
        list_cost = (t_in * p.input + t_out * p.output) / 1e6
        # spent = uncached*in + cached*cache_read + out*out (cache writes ignored: small)
        saved = list_cost - spent
        cached = saved / ((p.input - p.cache_read) / 1e6) if p.input > p.cache_read else 0.0
        out[agent] = {
            "trials": len(mine),
            "turns_per_trial": len(steps) / len(mine),
            "prompt_tokens_per_turn": t_in / len(steps),
            "output_tokens_per_turn": t_out / len(steps),
            "cached_share": max(0.0, min(1.0, cached / t_in)) if t_in else 0.0,
            "spent_usd": spent,
            "usd_per_trial": spent / len(mine),
        }
    sims = math.fsum(t.simulator_cost_usd for t in trajs)
    report = {"run": str(run), "agents": out, "simulator_usd": sims}
    dest = ROOT / "docs" / "measurements" / f"agent-tokens-{date.today().isoformat()}.json"
    dest.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    for a, m in out.items():
        print(
            f"{a:<20} {m['trials']:.0f} trials · {m['turns_per_trial']:.1f} turns/trial · "
            f"{m['prompt_tokens_per_turn']:.0f} in + {m['output_tokens_per_turn']:.0f} out per "
            f"turn · cached {m['cached_share']:.0%} · ${m['usd_per_trial']:.4f}/trial"
        )
    print(f"simulated users ${sims:.4f} · written {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
