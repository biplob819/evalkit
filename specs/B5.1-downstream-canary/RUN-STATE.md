# B5.1 run state

**Now:** GREEN (2026-11-17).

## History
- 2026-11-17 — node added with Wave 5 (OWNER D-61); widened to seven projects (D-62).
- 2026-11-17 — node_start GO; tests first in tests/downstream/ (registry, imports, canary); built `docs/downstream.tsv`, `scripts/downstream_imports.py`, `scripts/downstream_canary.py`, `.github/workflows/downstream.yml`.
- 2026-11-17 — first baseline: four pass; gemma-grounded-health fails on v0.2's `RunConfig.systems` (expected drift); pin-identity tests deselected by a new `deselect` column.
- 2026-11-17 — verifier pass 1: DSC-01 confirmed; DSC-02 refuted (public module imported whole classed internal; "venv" substring over-skipping); DSC-03 confirmed with defects (exit 5 as fail, denylist env, stored checkout token, permanently red jobs, stale deselect ids, stray-line parsing). All fixed: allowlist env with private HOME, `known` column and `known-drift`, `nothing-ran`, `tests=all` (fraud-review-bedrock now runs 965 tests), stale ids refused, only test_*.py files.
- 2026-11-17 — verifier pass 2: one new must-fix (a failing id with a space hid a real failure behind a known one) plus should-fixes; fixed: every failure must be named and known for `known-drift`, stdout-only anchored summary parsing, HOME per run, proxy/CA vars, node-id validation, plan checkout without credentials.
- 2026-11-17 — verifier pass 3 (real pytest output, 10 adversarial cases): DSC-03 CONFIRMED; two minor items fixed (teardown-error id not listed as fixed; plugin words in the summary line). Converge passed twice; baseline re-recorded on fresh clones: five pass, gemma known drift. GREEN.
