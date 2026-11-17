# B5.13 inputs (collected before the spec)

- fraud-review-bedrock (field-pm-b8, 2026-11-17): `rule_compliance` must read the cited rule ids from a configurable key of `decision` (theirs: `policy_clauses`) and take a configurable allow predicate (theirs: `action == "clear"`). Draft signatures accepted. Reference: src/fraudreview/policy.py, router.py `check_clear` (MIT).
- agentic-analytics (agentic-analytics-15): answer-from-rows and clarification from `src/agentic_analytics/scoring/` (S2.1, MIT); ~70 fixtures and 20 ambiguous questions as tests.
- Message field-pm-b8 and agentic-analytics-15 when this spec fixes the signatures.
