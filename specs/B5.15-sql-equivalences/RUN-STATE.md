# B5.15 run state

**Now:** verifier findings fixed, awaiting lead review (2026-11-17).

## History
- 2026-11-17 — lane opened (D-66); spec written from D-71 and agentic-analytics f6abdec.
- 2026-11-17 — built. `sql_match(..., question=None, equivalences=())`; rules in `evalkit/scorers/sql_equivalences.py`; 45 tests in `tests/scorers/test_sql_equivalences.py` (42 ported, all 8 baseline cases rescued, strict snapshot byte-identical). Gates: ruff check, ruff format --check, pyright (all extras), pytest (2079 passed, 5 skipped).
- Finding at build: strict `sql_match` failure details (`unmatched_*`, `example_gold_row`) depended on PYTHONHASHSEED (`_compare` iterated a set of groups). Fixed at merge (220e898): gold groups in gold row order. Snapshot values and reasons are unchanged from ca50e38; its failure details were deliberately regenerated once, and they are now deterministic (the same bytes under seeds 0, 1 and 12345).
- Port notes: the catalog's "17 percent/YoY fractions" are 3 percent cases (q-006, q-047, q-085) in the reference fixture, which has 8 cases in all; vendored as is.
- 2026-11-17 — verifier fixes (false passes shared with the reference f6abdec, now closed and stricter than it):
  - percent_scale: whole-word names (pct/percent/percentage; points+change only when percentage points are asked), so percentile and loyalty points are excluded; any negated percent disables the rule.
  - Per-column percent-unit tolerance (abs = max(abs_tol, 0.01) on the scaled column only); the `abs_tol >= 0.01` requirement is removed.
  - period_number: needs a gold column named month or quarter/qtr, bound to the converted column, and a year the question names.
  - empty_check: zeros only in count-named columns; other cells null or ISO dates.
  - pivot: an extra zero column must look like an omitted label (no summary word, not a gold column name, the labels' character kinds).
  - The unreachable `example_predicted_row` branch is removed (equal row counts mean a gold group always fails first).
  - Aligned with agentic-analytics D-91 (a638db9) by keeping the stricter rule wherever the two differ; its 7 tests are ported. 61 tests.
  - Fuzz: 4×50k against a638db9 and f6abdec. Every disagreement is EvalKit stricter, except percent_scale rescues at a caller `abs_tol < 0.01` (the per-column tolerance, by design).
  - criteria.md still carries the old SQE-01 wording (`abs_tol >= 0.01`, pct/percent/points); it is left for the lead with the catalog.
