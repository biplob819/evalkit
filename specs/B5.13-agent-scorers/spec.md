# B5.13 — Agent and decision scorers

**Wave 5. Depends on:** A2.2, S2.4. **Asked by:** agentic-analytics (its OWNER D-75), fraud-review-bedrock (its OWNER D-38). Decisions: D-61 to D-66.

Skeleton written by the lead so the lane can start (D-66); completed by the lane builder. Inputs collected before the spec are in `inputs.md`.

## Capabilities
- **AFR-01**: Answer-from-rows scorer: every number in an agent's answer appears in or is derivable as stated from its result rows (percent and ratio forms, dates in words, thousands separators, numbers in text cells or column names); agentic-analytics' fixtures are the tests [D-65]
- **CLR-01**: Clarification scorer: on ambiguous questions, did the agent ask back; on clear ones, the false clarification rate; both with intervals; agentic-analytics' 20 ambiguous questions are the tests [D-65]
- **RUL-01**: Rule-compliance scorer: a rule set (ids, kind, a machine-checked predicate over named fields, parsed by an AST allow-list, never eval); cited ids must exist, a cited allow rule must hold, no never-rule may hold on an allow; per-rule reasons; fraud-review-bedrock's cases are the tests [D-65]
- **SEV-01**: Structured-evidence faithfulness: a tabular record rendered as numbered evidence sentences so `faithfulness()` judges decisions, plus a deterministic value check of numbers and comparisons in each reason against the record within a tolerance; fraud-review-bedrock's cases are the tests [D-65]

## Goal
Score what agents and decision systems do, not only what they say: an analytics agent's numbers come from its result rows, it asks back when a question is ambiguous and not otherwise, a policy-bound agent cites rules that exist and hold, and its stated reasons match the record it decided on.

## Why
Two projects built these checks locally and both need them in EvalKit (D-65): agentic-analytics (`scoring/numbers.py`, `scoring/clarify.py`, S2.1) before its own v0.1.0, and fraud-review-bedrock (`policy.py`, `router.check_clear`, Tech Spec EVK-01/EVK-02) for its v0.3.0 pin. Each check is general to any analytics agent or policy-bound agent, so it is built once here, tested against the asking project's cases (D-61 rule 3), and every later project gets it.

## Interface
All names are exported from `evalkit.scorers` (public, semver; added to `scripts/check_public_api.py`). Each capability has plain functions for direct use and a registered scorer usable as a task scorer inside `evalkit run` (v0.2 protocol `f(case, completion, **ctx) -> ScoreResult`). Pure Python, no new dependency, no model calls.

### Answer from rows (AFR-01, `evalkit/scorers/rows.py`)
Port of agentic-analytics `scoring/numbers.py` (MIT, same author).
- `unsupported_numbers(answer: str, rows: ResultSet | None, question: str = "", *, top_n: int | None = None, year_range: tuple[int, int] | None = None, rel_tol: float = 0.005) -> tuple[str, ...]` — the numbers written in `answer` that nothing supports, as written, first-seen order, each once.
- `answer_from_rows(answer, rows, question="", *, top_n=None, year_range=None, rel_tol=0.005) -> ScoreResult` — 1.0 when every number is supported, else 0.0; details `unsupported`, `n_numbers`.
- A number is supported when: a numeric cell matches it to the precision displayed (half a unit of the last digit in its own scale: `190k` covers 189,500 to 190,500) or within `rel_tol`; it is a percent and the cell holds it in percent units, or as a fraction in a column named like `share|ratio|fraction|proportion|rate` (`pct|percent` columns: both readings); it is an `x` multiple equal to the ratio of two numeric cells in the same column (derivable as stated; skipped above 20,000 pairs); it appears in `question`; it is written inside a text cell or a column name (`FLAT 12`, a column `SALES 5000`, the year of a date cell `2024-03-31`); it is a bare year inside `year_range`, or a decade overlapping it; it equals `top_n` or the number of rows (plain integers only).
- Read: currency prefixes, thousands separators (standard, Indian, European), decimals, signs, `k m bn` and word scales, number words with a scale word (`half a million`), `%`, `percent`, `percentage points`, `pp`, `x` multiples, ranges. Not claims: dates in words and digits other than the year (`31 December 2024`, `2024-01-01`, `Q3 '24`), times, ordinals, list numbering, table ranks, `#1`, codes such as `M1`, fenced code.
- `year_range=None` (the default) makes bare years claims like any other number; agentic-analytics passes `(1995, 2026)`.
- Task scorer **`answer_from_rows`**: answer text `completion.answer.answer`, question `case.question`, rows `case.meta["result"]` (`{"columns": [...], "rows": [[...]]}`; absent means no rows), `case.meta["top_n"]`, `case.meta["year_range"]`. A failed call scores 0.0 with `reason`.

### Clarification (CLR-01, `evalkit/scorers/clarify.py`)
Port of agentic-analytics `scoring/clarify.py` (MIT).
- `clarification_rates(expected_clarify: Sequence[bool], asked: Sequence[bool], *, alpha: float = 0.05) -> ClarificationRates`.
- `ClarificationRates` (frozen model): `n_ambiguous`, `n_clear`, `asked` (asked back on ambiguous), `false_clarifications` (asked back on clear), `ask_rate: Interval | None`, `false_clarification_rate: Interval | None` (None when that side has no questions), `method = "clopper-pearson"`. Intervals are exact binomial (Clopper-Pearson) at 1 - alpha, the right interval at the n = 20 the asking project has (a bootstrap collapses to a point at 0/20 or 20/20).
- Task scorer **`asked_back`**: 1.0 when the answer's label is the clarify label (`case.meta["clarify_label"]`, default `clarify`, compared NFKC-casefolded), else 0.0. Details `expected` (`clarify` or `answer`, from `case.meta["expected"]`, else `clarify` when `reference_label` is the clarify label), `asked`, `false_clarification`. A failed call did not ask back. In a run, add `expected` to the task's `slices`: the slice `expected=clarify` gives the ask rate and `expected=answer` the false clarification rate, each with the run's bootstrap interval.

### Rule compliance (RUL-01, `evalkit/scorers/rules.py`)
Port of fraud-review-bedrock `policy.py` predicates and `Policy.clearing_allowed` (MIT).
- `Rule(id: Name, kind: Literal["allow", "never", "signal"], when: str, title: str = "")` and `RuleSet(rules: tuple[Rule, ...], fields: dict[str, FieldType])` (frozen pydantic models, JSON-loadable). `FieldType` is `"number"`, `"bool"`, `"text"` or a tuple of allowed strings (a category). Building a `RuleSet` parses and type-checks every predicate and refuses duplicate ids or a set with no allow rule (`RuleError`, a `ValueError`). `RuleSet.holds(rule_id, record) -> bool` evaluates one rule.
- Predicate grammar (AST allow-list; parsed with `ast.parse` and walked, never `eval`/`exec`/`compile`): `and`, `or`, `not`, chained comparisons `< <= > >= == !=`, field names, plain decimal numbers (optionally negative), `True`/`False`, quoted strings. Every comparison reads a field and compares values of one type; ordering only on numbers; a category constant must be one of its values; a bare name must be a bool field; ASCII, one line, single spaces, no comments, at most 300 characters.
- Missing values (absent, None, non-finite) fail safe: an allow rule does not hold, a never rule holds, a signal does not hold. A record value of the wrong type is a `RuleError`.
- `rule_compliance(decision: Mapping[str, Any], record: Mapping[str, Any], rules: RuleSet, *, cited_key: str = "rule_ids", allow: str | None = None) -> ScoreResult`. Cited ids are read from `decision[cited_key]`. `allow` is a predicate over the decision saying whether it is an allow (fraud-review: `action == "clear"`); `None` treats every decision as an allow; a decision whose allow fields are missing or of another type counts as an allow (checked, fail safe). Reasons, in order: `unknown_rule:<id>` for every cited id not in the set (on any decision), `bad_citations` when the key does not hold a list of strings; then, on an allow only: `no_allow_rule_cited`, or when no cited allow rule holds one `predicate_false:<id>` / `missing_value:<id>` per cited allow rule; then `never:<id>` for every never rule in the set that holds, cited or not. Value 1.0 with no reasons, else 0.0; details `allow`, `cited`, `reasons`, `rules` (per cited and never rule: kind, holds, missing fields).
- `rule_compliance_scorer(rules: RuleSet, *, name: str = "rule_compliance", cited_key: str = "rule_ids", allow: str | None = None, record_key: str = "record", version: str = "1") -> Scorer` registers a task scorer under `name` (not a built-in, so a project can ship it as an `evalkit.scorers` entry point). Record: `case.meta[record_key]`. Decision: `label`, `answer`, `citations` of `completion.answer`, plus every key of a JSON object written as the answer text. A failed call scores 0.0.

### Structured evidence (SEV-01, `evalkit/scorers/evidence.py`)
Built from fraud-review-bedrock Tech Spec §5.5 and EVK-02.
- `render_evidence(record: Mapping[str, Any], template: Sequence[str] | None = None) -> list[str]` — one sentence per template line, in order; sentence i is evidence sentence i (1-based) for `FaithfulnessItem.evidence`, which the judge prompt numbers itself. Placeholders: `{field}`, `{field:spec}` (a format spec for numbers, e.g. `.1f`) and `{field?text if true|text if false}` for bools; `{{`/`}}` are literal braces. Only plain field names are read: no attribute, index or call. A field whose value is None or non-finite turns its sentence into `<field> is not known.`; a field absent from the record is a `ValueError`. `template=None` renders `<field> is <value>.` per field in record order.
- `ValuePhrase(pattern: str, field: str | None = None, holds: str | None = None)`: a case-insensitive regex for a phrase in a reason, with exactly one of `field` (numeric: the number in the matched text, with an optional comparison word before it — more than, over, above, at least, under, below, less than, at most — is checked against that field) or `holds` (a condition: a predicate in the RUL-01 grammar that must hold on the record, e.g. `at night` → `hour < 5`).
- `value_check(reason: str, record: Mapping[str, Any], *, phrases: Sequence[ValuePhrase] = (), tolerance: float = 0.1) -> ScoreResult` — every phrase match is checked; every other number in the reason (read as in AFR-01) must equal some numeric field within the displayed precision or `tolerance` (relative; a percent may match the field as a fraction). Value 1.0 when nothing fails (also when nothing was checkable), else 0.0; details `checks` (text, kind, field or predicate, record value, ok), `failed`, `n_checked`. A missing record value fails the claim that needs it.
- `value_check_scorer(*, phrases=(), name="evidence_values", record_key="record", tolerance=0.1, version="1") -> Scorer`: record `case.meta[record_key]`; reasons are the `reasons` list of a JSON-object answer when present (each checked), else the answer text.

## Rules
- No model calls; deterministic; nothing executes text from a rule set, template, decision or reason.
- Every scorer returns a finite `ScoreResult` for every completion, failed calls included.
- Registered names are unique: `answer_from_rows` and `asked_back` are built-ins; factory scorers take the name given.

## Out of scope
- Re-executing an agent's SQL (agentic-analytics keeps `reexecute`) and router rules other than the policy (risk ceiling, confidence, version checks stay in fraud-review-bedrock).
- Policy file formats (fraud-review's `policy.md` subset stays there; it builds a `RuleSet` from its clauses).
- Phrase vocabularies for value checks (each project supplies its own `ValuePhrase` list).
- Changes to `faithfulness()` itself or its public promotion (B5.2); safety-bound statistics.
- Editing downstream repos (D-61 rule 4).
