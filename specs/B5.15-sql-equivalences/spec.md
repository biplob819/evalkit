# B5.15 — `sql_match` shape equivalences

**Wave 5 (v0.4.0). Depends on:** S2.1 (`sql_match`), D-55 (`allow_extra_columns`). **Asked by:** agentic-analytics (its OWNER D-81). Decisions: D-71 (admitted under D-63), D-61 rule 3 (the asking project's failing cases ship as EvalKit tests), D-66 (lane).

## Capabilities
- **SQE-01**: Opt-in `sql_match` equivalences, each off by default and named per call: `percent_scale`, `pivot`, `empty_check`, `period_number` (rules below; narrower than D-71's sketch, as the reference is) [D-71]
- **SQE-02**: Strict `sql_match` is unchanged by default (existing fixtures and outputs unchanged in values and reasons); results record `correct_strict` and the equivalences that matched, so strict and lenient scores sit side by side; agentic-analytics' baseline cases are tests [D-71]

## Goal
A SQL agent that returns the right answer in a different, defensible shape (a fraction for a percentage, one wide row for a long table, a zero-count check for "no data", period-start dates for period numbers) can be scored as right by a caller who asks for that, while the strict score stays beside it and nothing changes for callers who do not.

## Reference
agentic-analytics (MIT, same author), local commit `f6abdec` "OWNER D-81: tolerant headline scoring": `src/agentic_analytics/scoring/tolerant.py` (rules), `scoring/score.py` (strict first, `correct_strict`, `equivalences`), `scoring/result.py` (`match_rows`, the strict match the variants re-run), `tests/scoring/test_tolerant.py` (42 tests), `tests/scoring/fixtures/d81_baseline_cases.json` (8 real failing cases from baseline run-20261117T083854Z-c89eaa). The reference's rules are followed, not D-71's broader sketch.

## Interface
```python
sql_match(
    predicted: ResultSet,
    gold: ResultSet,
    *,
    rel_tol: float = 1e-6,
    abs_tol: float = 1e-9,
    allow_extra_columns: bool = False,
    question: str | None = None,
    equivalences: Collection[str] = (),
) -> ScoreResult
```
- `equivalences`: names from `evalkit.scorers.sql_equivalences.EQUIVALENCES` = `("percent_scale", "pivot", "empty_check", "period_number")`. Any other name, or a bare string, raises `ValueError`/`TypeError`. Empty (the default) gives the pre-B5.15 values and reasons; failure details were made deterministic across hash seeds and regenerated once.
- `question`: the question text; `percent_scale` and `period_number` read it and never apply when it is `None`. Not a `str` or `None` -> `TypeError`.
- The strict match runs first. If it passes, that result is returned with `correct_strict: true, equivalences: []` added to `details`. If it fails, each normalised form of `predicted` (below, in the order listed) is matched by the same strict match with the same `rel_tol`, `abs_tol`, `allow_extra_columns` and `gold.ordered`; the first that passes gives `value 1.0` with its own details plus `correct_strict: false`, `equivalences: [names used]`, `strict_reason`. If none passes, the strict failure is returned with `correct_strict: false, equivalences: []` added. These keys appear only when at least one equivalence is named.

## Rules (each a pre-normalisation of the predicted rows; the gold is never changed)
Stricter than the reference (verifier, 2026-11-17): an equivalence may only reshape a right answer. A converted column must be compared with a gold column of its kind (the variant binds it; any other column mapping fails). Column names are read as whole words (`[a-z]+|[0-9]+` after casefolding); words that must *name* a column are not split at camelCase, words that *exclude* one also are. Where agentic-analytics' D-91 narrowing (a638db9) differs, the stricter rule is kept.
1. **percent_scale.** The question asks for a percentage (`percent`, `percentage`, `per cent`, `%`; not `percentile`, `share`, `proportion`) and nowhere negates one (`not`/`no`/`never`/`without`/`rather than`/`instead of`/`non-`, then up to three of a/an/as/in/into/the/expressed/given/shown/returned/reported, then `%`/percent/percentage). A gold column has a word `pct`, `percent` or `percentage` in its name, or both `points` and `change` when the question asks for percentage points (`percentile`, `pcnt`, `rate`, `avg_loyalty_points` do not count). A predicted numeric column is multiplied by 100 when, so scaled, its values fit such a gold column (same non-numeric values, same count, sorted pairing within the percent-unit tolerance) and, unscaled, they do not. The scaled column alone is compared with `abs` = max(`abs_tol`, 0.01) and the caller's `rel_tol` (the reference's per-column D-51 tolerance); every other column keeps `abs_tol`. No `abs_tol >= 0.01` is required.
2. **pivot.** The gold is long: two columns, one all-text label column and one all-numeric value column, at least one row, distinct labels. The prediction is one row whose column names are the gold's labels (case-insensitive; spaces, `-` and `_` are one separator). Every gold label must be a predicted column. Any other predicted column must hold a numeric zero and look like an omitted label: no summary/metric word (`SUMMARY_WORDS`: total, totals, subtotal, sum, all, overall, grand, count, counts, cnt, n, num, avg, average, mean, median, min, max, pct, percent, percentage, share, ratio, rate), not a gold column name, and only the character kinds (letter, digit, separator, other) the gold's labels use. The row is unpivoted into the gold's columns.
3. **empty_check.** The gold has no rows and the prediction is one row that is a zero count with only dates and nulls beside it: at least one number (numbers, and text that is a plain number), every number zero and in a count column (a word count, counts, cnt, n, num, number, rows, records, sales, transactions, orders or items; none of avg, average, mean, median, min, max, sum, price, amount, value, revenue, pct, percent, percentage, ratio, rate, share), every other cell null or an ISO date/timestamp. `total_sales` counts; `total`, `total_revenue`, `avg_price` and error text do not. Naming `empty_check` is the caller's statement that the question expects no data (agentic-analytics' `expects_empty`).
4. **period_number.** A predicted column of dates that are all month starts (`YYYY-MM-01`, optionally midnight with `T`/space, `Z` or `+00:00`) in one calendar year becomes the month number (1–12) when the question names months (`month`/`months`) and a gold column named with the word `month` is all integers in 1..12, or the quarter number (1–4) when every date is a quarter start, the question names quarters and a gold column named with `quarter` or `qtr` is all integers in 1..4; the converted column must be compared with that column. When the question names years (`19xx`/`20xx` not inside a longer number), the dates' year must be one of them. Dates over two or more years never convert.

`percent_scale` and `period_number` act on columns and may apply together (tried after each alone, recorded as `["percent_scale", "period_number"]`); `pivot` and `empty_check` reshape the whole result and apply alone. Order tried: empty_check, pivot, percent_scale, period_number (month then quarter), percent_scale+period_number; only the named ones.

## Differences from the reference (EvalKit has no `Question`)
- `sql_match` has one `abs_tol`/`rel_tol`; the only per-column tolerance is the scaled percent column's percent-unit `abs` (max(`abs_tol`, 0.01)), so a caller with an exact tolerance gets a percent rescue with every other column exact. The reference instead needs the question's tolerance to cover the column with `abs >= 0.01`; with `abs_tol < 0.01` EvalKit rescues where the reference declines (the one way EvalKit is more lenient, by design).
- The reference's later text month/quarter labels ("March 2016", "Q1") are not ported.
- `expects_empty`, "answer-expected only", probes, answer errors and truncation are the caller's: it names `empty_check` only for questions expecting no data and calls `sql_match` only on rows it executed in full.
- The strict match is EvalKit's own (`sql_match`); agentic-analytics' column assignment by value profile is not ported. The baseline cases pass with `allow_extra_columns=True` (as agentic-analytics calls it) and the question's tolerance.

## Test plan
- `tests/scorers/test_sql_equivalences.py`: the 42 reference tests ported (the 8 baseline cases parametrised; fixture coverage; per-rule positives and false-pass negatives incl. share/proportion/percentile, non-percent names, non-zero extras, swapped pivot gold, two-year dates, unnamed quarters, ordered rescues, strict-first labelling) with EvalKit's arguments in place of agentic-analytics' `Question`/`Scores` (its clarify/probe/error tests become off-unless-named, question-gating and miss-keeps-strict tests; its record-format tests become the snapshot, details round-trip and argument-validation tests), plus three: order-free determinism, unchanged in values and reasons results across hash seeds in fresh interpreters, hostile inputs. 45 tests at build; the verifier fixes add 16 (per-column percent tolerance, the stricter-than-reference false passes from the verifier's probe, agentic-analytics D-91's 7 tests ported, rows_differ reachability): 61 collected.
- `tests/fixtures/sql_equivalences/d81_baseline_cases.json`: vendored verbatim from the reference (aggregate counts and shares over HM Land Registry Price Paid data, town names only; no personal data).
- `tests/fixtures/sql_equivalences/strict_snapshot.json`: `sql_match` outputs (JSON) on every `sql_match_cases.json` case and every baseline case, with and without `allow_extra_columns`, values and reasons from `ca50e38`, failure details regenerated once after they were made deterministic; the default call reproduces them byte for byte under any hash seed.

## Out of scope
Changing `sql_match` defaults; a task-scorer wrapper; caller-set per-column tolerances; any change in the agentic-analytics repo (D-61 rule 4).
