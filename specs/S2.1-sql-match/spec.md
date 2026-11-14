# S2.1 — `sql_match` scorer

**Wave 2. Depends on:** A2.2. **Unblocks:** C3.4 (agent outcome scoring for analytics tasks).

## Goal
Decide whether a query's result set matches the gold result set, the way an analyst would: same rows and values, regardless of column aliases, column order or (unless order matters) row order, with numeric tolerance.

## Interface
`from evalkit.scorers import sql_match` — `sql_match(predicted: ResultSet, gold: ResultSet, *, rel_tol=1e-6, abs_tol=1e-9) -> ScoreResult` (1.0 match, 0.0 otherwise; `details.reason` says why). It compares result sets, so it is a plain function rather than a `(case, completion)` Scorer.

## Rules
- Columns: if the predicted column names are a case-insensitive permutation of the gold names, reorder to gold order; otherwise compare positionally, which needs the same column count.
- Rows: a multiset comparison (duplicates matter); if `gold.ordered`, the sequence must match.
- Values: numbers (int, float, numeric strings are not coerced) compare with `math.isclose(rel_tol, abs_tol)`, `True`/`False` are not numbers, `None` equals only `None`, strings compare exactly after trimming trailing whitespace.
- An empty gold set matches only an empty prediction.
