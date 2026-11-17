# B5.13 acceptance criteria

**AFR-01** Answer-from-rows scorer: every number in an agent's answer appears in or is derivable as stated from its result rows (percent and ratio forms, dates in words, thousands separators, numbers in text cells or column names); agentic-analytics' fixtures are the tests [D-65]
**CLR-01** Clarification scorer: on ambiguous questions, did the agent ask back; on clear ones, the false clarification rate; both with intervals; agentic-analytics' 20 ambiguous questions are the tests [D-65]
**RUL-01** Rule-compliance scorer: a rule set (ids, kind, a machine-checked predicate over named fields, parsed by an AST allow-list, never eval); cited ids must exist, a cited allow rule must hold, no never-rule may hold on an allow; per-rule reasons; fraud-review-bedrock's cases are the tests [D-65]
**SEV-01** Structured-evidence faithfulness: a tabular record rendered as numbered evidence sentences so `faithfulness()` judges decisions, plus a deterministic value check of numbers and comparisons in each reason against the record within a tolerance; fraud-review-bedrock's cases are the tests [D-65]
