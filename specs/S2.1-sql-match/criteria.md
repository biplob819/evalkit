# S2.1 acceptance criteria

**SQL-01** `sql_match` returns 1.0 for identical sets; ignores row order unless `gold.ordered`; respects duplicates; reorders columns when names are a case-insensitive permutation; compares positionally otherwise; tolerates float error within `rel_tol`/`abs_tol`; treats booleans, None and strings strictly; reports a reason for every mismatch.
**SQL-02** Golden fixtures (`tests/fixtures/sql_match_cases.json`) cover at least 12 named cases (aliasing, column permutation, duplicate rows, ordering, float tolerance, int-vs-float, None, empty sets, extra/missing rows and columns), each with the expected score; `sql_match` is exported from `evalkit.scorers`.
