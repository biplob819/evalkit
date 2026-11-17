# B5.6 run state

**Now:** GREEN (2026-11-17).

## History
- 2026-11-17 — spec written (D-66 lanes).
- 2026-11-17 — lane build: exact interface written into spec.md, tests first (tests/test_compare.py, fixtures in tests/fixtures/compare), `evalkit.compare` and `evalkit compare`, docs/compare.md; ruff, ruff format, pyright and the full suite green. Next: converge on main after the merge, then the verifier.
- 2026-11-17 — built in its lane on the spec written ahead; verifier pass 1 refuted RVR-01/02 on small samples, partial overlap and count sidecars: fixed (MIN_CASES 10, MIN_OVERLAP_SHARE 0.5, sidecar checks). The lead changed one rule: a paired row with ≥ 10 shared cases that all moved alike is a claim, not degenerate. Verifier pass 2: all confirmed, no must-fix; rate tolerance given float slack (3/128 at 6 decimals), unchanged rows say so. Documented limits: paired p-values bottom out near 2e-4, so a uniform change cannot be flagged once 250+ rows share the Holm family; a held-out group of one shows its flag count. GREEN.
