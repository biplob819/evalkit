# B5.15 run state

**Now:** BUILT, awaiting verifier (2026-11-17).

## History
- 2026-11-17 — lane opened (D-66); spec written from D-71 and agentic-analytics f6abdec.
- 2026-11-17 — built. `sql_match(..., question=None, equivalences=())`; rules in `evalkit/scorers/sql_equivalences.py`; 45 tests in `tests/scorers/test_sql_equivalences.py` (42 ported, all 8 baseline cases rescued, strict snapshot byte-identical). Gates: ruff check, ruff format --check, pyright (all extras), pytest (2079 passed, 5 skipped).
- Finding (pre-existing, not changed): strict `sql_match` failure details (`unmatched_*`, `example_gold_row`) depend on PYTHONHASHSEED (`_compare` iterates `set(groups_p) | set(groups_g)`, tuples of strings), and `example_gold_row` shows a predicted row when the failing group has no gold rows. Values and reasons are stable. The snapshot is compared at seed 0.
- Port notes: the catalog's "17 percent/YoY fractions" are 3 percent cases (q-006, q-047, q-085) in the reference fixture, which has 8 cases in all; vendored as is.
