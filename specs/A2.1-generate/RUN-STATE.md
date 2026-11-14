# A2.1 run state

**Now:** green (real 20-case smoke run pending: owner is adding API keys, D-30)
**Next:** run `uv run --all-extras python scripts/smoke_run.py --n 20 --yes` once both keys are set; feed docs/measurements into the cost model

## History
- 2026-11-14 — built; verifier found a blocker (budget exhaustion recorded as transport) and disproved GEN-02, GEN-03; all fixed (L-21, validate v2).
- 2026-11-14 — converge passed twice; rows flipped green.
