# B5.13 run state

**Now:** GREEN (2026-11-17).

## History
- 2026-11-17 — lane opened (D-66).
- 2026-11-17 — merged; five verifier passes. Pass 1 refuted AFR-01 (x-multiples too generous; cubic number words; answer JSON overriding the decision; untyped predicates raising): fixed. Pass 2: percent vs fraction fields in comparisons; fixed. Passes 3–5 narrowed negation handling until it became fail-safe: only a negation directly before the comparison inverts it, any other in the sentence makes the check ambiguous and it fails. value_check never raises on record contents (fuzzers: 0 raises). rule_compliance agrees with fraud-review on 5,000 records; answer_from_rows with all 92 agentic-analytics fixtures. GREEN.
