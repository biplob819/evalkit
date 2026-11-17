# Scorers and model adapters

## Models without a plugin

`evalkit run` routes each model in `evalkit.yaml` by name: `claude-*` goes to Anthropic,
OpenAI model ids (`gpt-*`, `o1`, `o3`, ...) go to OpenAI, and anything listed under `endpoints` goes to
an OpenAI-compatible server. That covers self-hosted servers that need no API key (vLLM,
Ollama, a local LiteLLM proxy and similar) with no code; endpoints that need a key are not
supported from the config yet:

```yaml
models: [claude-haiku-5-5, my-llama]
endpoints: {my-llama: http://localhost:8000/v1}
```

An endpoint model with no price in `prices.yaml` is costed at $0 with a warning; any other
model without a price is refused before the run starts.

## Writing a scorer

A scorer is a function of a case and a completion that returns a `ScoreResult` (a value plus
a details dict). The `@scorer` decorator registers it under a unique name and version.

```python
from evalkit.scorers import ScoreResult, scorer


@scorer("answer_length", version="1")
def answer_length(case, completion, **ctx):
    """Words in the answer; 0 when the model gave none."""
    if completion.answer is None:
        return ScoreResult(value=0.0, details={"reason": "no_answer"})
    return ScoreResult(value=float(len(completion.answer.answer.split())))
```

To ship it as a package, declare an entry point in your `pyproject.toml`:

```toml
[project.entry-points."evalkit.scorers"]
answer_length = "my_package.scorers:answer_length"
```

`evalkit.scorers.discover()` then returns it next to the built-ins, and
`get_scorer("answer_length")` finds it. Names are unique: a plugin cannot replace a
registered scorer (`label_correct`, `citation_valid`, `cost`, `latency`), and a plugin that fails to import is skipped with a warning instead of breaking the
built-ins. Bump `version` whenever the scorer's output changes, so cached scores are not
mixed with new ones.

The built-in scorers are a public, versioned API that you can use in your own pipelines:
`label_correct`, `citation_valid`, `cost`, `latency`, `sql_match`, `attack_outcome`,
`recall_at_k`, `mrr` and `ndcg_at_k`, and since 0.3.0 the agent and decision scorers below
(see [versioning.md](versioning.md)).

### Running your scorer inside a run (0.2.0)

Name it in the task's `scorers` list. The benchmark set (`label_correct`, `citation_valid`,
`groundedness`, `coverage`, `cost`, `latency`) is computed by the pipeline as before; every
other name is a **task scorer**:

```yaml
scorers: [label_correct, citation_valid, groundedness, coverage, cost, latency, answer_length]
```

- Every task scorer is looked up when the run is planned; an unknown name exits 2 before any
  model is called.
- After judging, it is called once per answer as `scorer(case, completion, model=...,
  claims=..., verdicts=..., scores=...)`, so accept `**ctx`. `case.meta` holds any per-case
  data from the dataset. It must return a `ScoreResult` with a finite value, for every
  answer, failed calls included (a failed call has `completion.answer is None`).
- The value lands in `records.jsonl` (`task_scores`, with the scorer's version and details),
  in `summary.json` (`task_scores` per model: the mean over all cases with a 95% bootstrap
  interval) and as a column in `report.html`.
- A scorer that raises stops the run with exit 2, naming the scorer and the case. Model calls
  already made are cached, so fixing the scorer and rerunning costs nothing.
- Task scorers are local functions. A scorer that needs a model call (a judge) is not
  supported inside a run yet.

### Agent and decision scorers (0.3.0)

Four checks for agents that compute answers or make policy-bound decisions. Each has a plain
function and a task scorer; none calls a model.

- **Answer from rows.** `unsupported_numbers(answer, rows, question, *, top_n=None,
  year_range=None, rel_tol=0.005)` lists the numbers in an answer that its result rows
  (a `ResultSet`) do not back: cells to the precision written (`190k`, `£1.2m`), percents
  against percent or share columns, `6.6x` as the ratio of the cells of two rows the sentence
  names ("Kensington is 6.6x Manchester"; with fewer than two named rows a multiple needs a
  matching cell), numbers inside text cells and column names, the question, `top_n` and the
  row count. A number written with a minus (`-4%`) needs a negative cell; an unsigned one
  ("fell 3.2%") may match either sign. Dates in words, times,
  codes such as `M1` and list numbering are not claims; bare years are, unless you pass
  `year_range`. `answer_from_rows(...)` returns 1.0 or 0.0 with `unsupported` in the details.
  The task scorer `answer_from_rows` reads the rows from `case.meta["result"]`
  (`{"columns": [...], "rows": [[...]]}`), plus `top_n` and `year_range` from `case.meta`.
- **Clarification.** `clarification_rates(expected_clarify, asked)` returns the ask rate on
  ambiguous questions and the false clarification rate on clear ones, each with an exact
  (Clopper-Pearson) 95% interval. The task scorer `asked_back` is 1.0 when the answer's label
  is `clarify` (or `case.meta["clarify_label"]`); add `expected` (`clarify` or `answer`) to
  each case's meta and to the task's `slices` to see both rates in the summary.
  The two give different intervals: `clarification_rates` gives exact Clopper-Pearson
  intervals, while run slices in `summary.json` use the run's percentile bootstrap for every
  task score. At small slice sizes, and at 0/n or n/n where the bootstrap collapses to a point,
  report the exact interval from `clarification_rates` (computed from `records.jsonl`).
- **Clustered clarification intervals (0.4.0, agentic-analytics D-100).** With k trials per
  question the answers are not independent (one question's answers move together), so
  Clopper-Pearson over answers is too narrow. Pass one question id per answer:
  `clarification_rates(expected_clarify, asked, cluster=question_ids, seed=0,
  n_resamples=10_000)`. Each rate is then the mean of per-question rates (asked / n when every
  question has the same number of answers) with the two-level cluster bootstrap that
  `evalkit compare` uses (`evalkit.stats.two_level_interval`: resample questions, then answers
  within each; D-41, D-51), seeded and reproducible; `method` is `cluster-bootstrap`, and
  `n_ambiguous_questions`, `n_clear_questions`, `seed` and `n_resamples` are filled in. With
  one answer per question nothing is clustered and the exact intervals are kept. Without
  `cluster` the result is byte-identical to 0.3. Two things to know: like any bootstrap, a
  rate whose answers all agree (0 of n) gets a zero-width interval, so quote
  `evalkit.stats.clopper_pearson(0, n_questions)` there; and with independent trials the
  two-level interval is conservative (1.2-1.35x the Clopper-Pearson width, never narrower;
  simulated 95% intervals covered 97.8-99.3% of the time at k ≥ 3 with no within-question
  correlation). With correlation it holds its level where Clopper-Pearson does not: at
  correlation 0.3 the bootstrap covered 95-99% against Clopper-Pearson's 83-92%, and at 0.8
  93-97% against 68-83% (400 replications per cell, true rate 0.3). On a 17-configuration fixture it agrees with agentic-analytics'
  `aggregate.two_level_interval`: equal points, interval ends within one step of the rate
  lattice, 1/(k x questions). To put it in a metrics-1 file, carry the interval and its
  method; `evalkit gate` then uses it as given instead of computing Clopper-Pearson from k
  and n:

  ```python
  r = clarification_rates(expected, asked, cluster=question_ids)
  iv = r.ask_rate
  Metric(name="clarification_rate", unit="rate", value=iv.point, n=r.n_ambiguous,
         ci_low=iv.lo, ci_high=iv.hi, ci_method=r.ci_method, labels={"model": model})
  ```

  Add `k=r.asked` only when every question has the same number of answers (metrics-1 needs
  value = k / n when `k` is given). EvalKit's own metrics-1 writers: `evalkit gate`'s
  conversion of a run's `summary.json` has one answer per case (no trial level, nothing to
  cluster); the security kit's `attack_trial_success_rate` has trials per case and takes
  the same option (`summarise(..., cluster_trials=True)`, [security.md](security.md#outputs-and-gates)).
- **Rule compliance.** Build a `RuleSet` from `Rule(id, kind, when)` with kind `allow`,
  `never` or `signal`, over typed fields (`"number"`, `"bool"`, `"text"` or a list of
  category values). Predicates such as `amount < 50 and not merchant_first_seen` are parsed
  with an allow-list and never executed. `rule_compliance(decision, record, rules,
  cited_key="policy_clauses", allow='action == "clear"')` checks that every cited id exists,
  that on an allow a cited allow rule holds and that no never rule holds, with one reason per
  failed rule (`unknown_rule:P42.1`, `predicate_false:P7.1`, `never:P3.9`). Missing values
  fail safe; a category value outside the field's declared values is a `RuleError`. In the
  `allow` predicate text compares case-insensitively (`"Clear" == "clear"`) and only numbers
  can be ordered: ordering anything else adds `type_error:allow` and fails the decision instead
  of raising. `rule_compliance_scorer(rules, name=..., cited_key=..., allow=...)` registers a
  task scorer that reads the record from `case.meta["record"]` and the decision from the
  answer: its label, answer and citations, plus the other keys of a JSON-object answer text.
  The structured label wins over a `label` key in the JSON, and the decision counts as an
  allow when either says allow, so the never rules are checked.
  When the allow predicate reads a field (e.g. `action == "clear"`), a structured `label`
  that says clear does not override an answer JSON whose `action` says hold: the decision is
  read from the field the predicate names.
- **Structured evidence.** `render_evidence(record, template)` turns a record into evidence
  sentences for `FaithfulnessItem.evidence`, so `faithfulness()` judges each reason against
  the record (`"The amount is {amt_vs_usual} times the card's usual spend."`,
  `"{merchant_first_seen?First purchase at this merchant.|A known merchant.}"`).
  `value_check(reason, record, *, phrases=(), tolerance=0.0, aliases=None, units=None,
  constants=(), ignore=(), unreadable=())` checks the numbers in a reason against the record's fields,
  strictly (0.4.0, B5.16, D-74): see [Strict binding](#value_check-strict-binding-d-74).
  Each `ValuePhrase(pattern, field=...)` binds the one number in its match to that field
  ("9x usual spend"), and each `ValuePhrase(pattern, holds="hour < 5")` checks a condition
  ("at night"). Conditional texts in templates may hold colons (`{flag?yes: really|no}`).
  `value_check_scorer(phrases=..., name=..., aliases=..., units=..., ...)` checks each string
  in a JSON answer's `reasons` list; each check carries `reason_index`, the position of its
  reason in the list. Each reason is verified, contradicted or unverifiable; see
  [Three outcomes](#value_check-three-outcomes-d-72).

```python
from evalkit.scorers import Rule, RuleSet, rule_compliance_scorer

RULES = RuleSet(
    fields={"amount": "number", "merchant_first_seen": "bool", "risk_score": "number"},
    rules=(
        Rule(id="P7.2", kind="allow", when="amount < 50 and not merchant_first_seen"),
        Rule(id="P8.9", kind="never", when="risk_score >= 0.9"),
    ),
)
policy_check = rule_compliance_scorer(
    RULES, name="policy_check", cited_key="policy_clauses", allow='label == "clear"'
)  # ship it under the `evalkit.scorers` entry-point group, then list `policy_check` in a task
```

#### value_check: a structured-facts check (0.4.0, D-74, D-77)

`value_check` verifies plain "field is value" statements only, and every other number in a reason makes it unverifiable. To get coverage, ask your model to end each reason with a short facts line built from field names, for example: "Facts: Amount is 812.40. Hour is 3. Risk score is 0.83." The check verifies such a line reliably. Prose around it, such as "a large late-night purchase", is not read and does not need to be. Put the facts line in its own reason string, or in its own sentence, so that no other number shares its clause.


`value_check`, `value_check_scorer`, `value_check_shares`, `ValuePhrase` and
`ValueCheckShares` are **not part of the 0.3.0 public API** (OWNER D-74). They failed seven
independent verification passes: when the checker could not tell which field a number was
about, it could match the number against any field close in value, so some false claims came
back `verified`. Node B5.16 redesigns it for 0.4.0 under one rule: `verified` only when a
whole field name or alias is bound to its number by a recognised pattern; every other reading
is `unverifiable`. `from evalkit.scorers import value_check` fails in 0.3.0. The code stays in
the internal module `evalkit.scorers.evidence`, which may change in any release; do not gate
on it. Until 0.4.0, judge reasons against the record with `render_evidence` and
`faithfulness()` (above), or keep your own checker (fraud-review-bedrock does). The text below
describes the 0.3 development behaviour that B5.16 replaces.

`value_check(reason, record, *, phrases=(), tolerance=0.1, aliases=None, constants=(),
ignore=())` checks the reason's numbers against the record's fields, and each
`ValuePhrase(pattern, field=...)` or `ValuePhrase(pattern, holds="hour < 5")` you declare
("9x usual spend", "at night"). A comparison word before a number ("above 900", "at least
3") is checked as that comparison; a minus sign must match a negative value; a phrase over
a field the record lacks is unchecked with `missing_value:<field>`.
`value_check_scorer(phrases=..., name=..., aliases=..., constants=..., ignore=...)` checks
each string in a JSON answer's `reasons` list; each check keeps its own `reason`
(`missing_value:...`, `type_error:...`, `no_field`, `ambiguous_negation`) and carries
`reason_index`, the position of its reason in the list. Each reason is verified,
contradicted or unverifiable; see [Three outcomes](#value_check-three-outcomes-d-72-development-not-public).

#### value_check: three outcomes (D-72, development, not public)

Every reason gets one outcome, in `details["outcome"]`:

| Outcome | When | `value` |
| --- | --- | --- |
| `verified` | at least one check, and every check is `ok` | 1.0 |
| `contradicted` | at least one check `failed`: a bound statement the record clearly says otherwise about | 0.0 |
| `unverifiable` | no check failed, but a number is unbound, a check is ambiguous or unchecked, or nothing in the reason was checkable | 0.0 |

Each check carries `status` and `reason`:

- `ok`: the bound claim holds.
- `failed`: the bound claim is clearly false ("risk score is 0.3" on 0.83, "card history is 44"
  on 40, "rare category" declared as `category_freq < 0.05` on 0.07).
- `unbound`: a number the text does not bind to exactly one field. `reason`: `no_field`,
  `unclear_subject`, `unclear_field`, `unclear_comparison`, `unit`, `hedged`, `time`,
  `malformed`, `constant`, `number_word` or `non_ascii_number`.
- `ambiguous`: bound, but the reason may not assert it: `negation`, `hypothetical`, `denial`,
  `respectively`, `question`, `unclear_comparison` or `unreadable` (anywhere in the reason),
  `unclear_field`, `rule_id` or `block_word` (in its clause), or `rounding` and
  `coarse_rounding` (a correct rounding of the value, too coarse to verify).
- `unchecked`: the record cannot settle it: `missing_value:<field>`, `type_error:<field>`, or
  `unit:<field>` (a bare number above 1 on a `fraction` field).

Details also hold `checks` (kind, text, field, op, claimed, found, ok, status, reason),
`failed` and `unverified` (the texts of failed checks and of all others not ok), `n_checked`
(every number, boolean and condition read) and `unchecked` (numbers inside `ignore` spans).

#### value_check: strict binding (D-74)

`verified` only when every number-bearing statement in the reason is bound to a field by
positive evidence and holds. No number is ever matched to a field because its value is close.

- **Field names.** A field is named by its whole record key or an alias you declare
  (`aliases={"card_history_n": ["card history", "earlier purchases"]}`), case-insensitive,
  with spaces and underscores interchangeable (`risk_score` = "risk score" = "RISK_SCORE" =
  "riskscore"). Partial names ("card history" for `card_history_n` without the alias),
  plurals, hyphenations, typos and other subjects ("price", "total") name nothing.
- **Shapes.** A number binds to a field only in these shapes, with nothing else between:
  `<field> is|was|of|at|=|:|equals|totals <n>`, `<field> (<n>)`, `<field> <n>`,
  `<field> [is|was] <cmp> <n>` with cmp one of above, over, below, under, exceeds, more /
  greater / higher / less / fewer / lower than, at least, at most, at or above, at or below,
  `>`, `<`, `>=`, `<=`, `≥`, `≤`; `[<cmp>] <n> <field>` ("More than 30 earlier
  purchases."); and the one number inside a `ValuePhrase(field=...)` match (a comparison
  word right before it is read). A phrase's number, or a right-hand field's, binds nothing
  when another field is named right before it or anywhere in its clause: "Risk score is 9.1x
  usual" and "Amount is 9.1x the card's usual" bind nothing to `amt_vs_usual`. Fields joined
  by and / or / `,` / `/` / `&` ("Risk score and hour: 3"), a field on both sides of a
  number, a field named for two numbers, and a number bound to two fields bind nothing. "41
  or more" is not read as a comparison: the reason is unverifiable.
- **Framed statements (closed world, verify516b).** A bound statement counts only when the
  words right around it are understood. It starts a clause: the start, a line break, a
  line-start bullet, or a sentence end (`.` `;` `!` `?`, not an abbreviation's full stop such
  as "e.g." or "avg."); never a comma, colon, dash, bracket or other word. Only an article or
  a possessive may stand before the field name ("the", "a", "its", "the card's" when "card"
  is a word of that field's own name or alias). It ends a clause: the end, a line break or a
  sentence end; or "and", "," or ", and" joins it to another framed statement ("Amount is
  $812.40, and hour is 3."). A phrase match must be the whole statement ("9.1x usual."
  binds; "Spend is 9.1x usual" does not: declare the subject in the pattern if you need it).
  Anything else leaves the statement unbound (`unframed`): "Amount is $812.40, which is
  high", "Risk score is 0.83 (high)", "For the refund, amount is $812.40", "For the merchant:
  card history is 40", "The card has 40 earlier purchases", "The merchant, with 40 earlier
  purchases, is new", "Flag is true (for the previous card)", and every hedge spelling after
  a number ("900-ish", ", est.", "(rounded)", "±5", "+/- 5", ", give-or-take").
- **Every numeric token is read (closed world, verify516b).** Each run of digits anywhere in
  the reason (glued to letters, underscores or currency codes as in "USD900", "h8", "P7.1";
  inside markdown or quotes as in "_0.5_" or "`70`"; in any script, or a character whose
  NFKC form holds a digit, such as "８", "½" or "²") and each number word ("forty",
  "single", "couple", "pair", "once", "twice", "few", "several", "many", "first", "half",
  "dozen", ...) must be consumed by exactly one framed binding. Any other is an unbound
  check (`id`, `unread_number`, `non_ascii_number`, `number_word`, `double_bound`), so the
  reason is unverifiable. The only exemptions are spans matched by `ignore=[regex, ...]`
  (declare rule ids there, e.g. `r"\bP\d+(?:\.\d+)*\b"`) and the field names themselves; a
  `constants` value is still unbound (D-74). A condition phrase's number words ("First
  purchase at this merchant.") are consumed only when the phrase is a framed statement.
- **Clauses.** A clause runs between sentence ends, commas, brackets, dashes, line breaks and
  joining words (and, but, while, whereas, so, which, yet, because, although, though, since).
  A clause whose checks bind two fields ("amount 11.01 is 0.21 times the card's usual"),
  hold a rule id ("Amount is 11.01 under P3.1"), or keep a threshold, baseline or range word
  outside every phrase match ("limit", "threshold", "signal", "rule", "usual", "average",
  "between", "each", "+") has those checks ambiguous.
- **Units.** Declare them per field: `units={"risk_score": "fraction", "amt_vs_usual":
  "multiple", "amount": "currency", "hour": "hour"}`. `fraction` (field 0 to 1; "83%" is
  0.83), `percent` (field 0 to 100), `multiple` ("9x", "9 times", "9-fold"), `currency`
  ("$812.40", "812.40 USD"), `hour` (field 0 to 23; "3 a.m.", "3pm", "15:00"; other
  minutes are unbound). Without a declared unit only a bare number binds: "risk score is 83%"
  is unverifiable. On a `fraction` field a bare number above 1 ("category freq is 3.8") is
  unchecked. Scales and suffixes ("5k", "thousand", "cents", "3rd", "1e9") never bind.
  A phrase-bound number may carry a multiple or currency mark.
- **Tolerance.** Integer fields (an `int` in the record) are exact, whatever `tolerance`
  says. A non-integer field equals a claim when it rounds to the claim at the claim's
  displayed precision and is within 0.005 or 0.5% of it, and at most 10% off ("risk score is
  0.83" on 0.8312 holds; "risk score is 0.8" on 0.83 and "9x" on 9.1 are correct but coarse
  roundings, unverifiable), or within `tolerance` (relative, default 0, for non-integer fields
  only). One unit off in the last digit shown is a truncation ("18.21 times" on 18.215),
  unverifiable; more is a failure. Comparisons are exact. These are fraud-review-bedrock's
  D-42 tolerances where they are the stricter.
- **Never verified.** Approximate words ("about", "roughly", "~", "or so"), ranges
  ("between", "800-900"), lists with "respectively", times with no hour field, numbers in
  words ("forty", "twice", "midnight"), non-ASCII digits, malformed or very long numbers, and
  policy constants ("the 0.9 threshold": `constants` only label them) are unbound. Ids glued
  to letters ("P7.1") are unbound too unless an `ignore=[regex, ...]` span covers them;
  numbers inside `ignore` spans are ids, listed in `unchecked`, but negations there are still
  read. A negation (not, no, never, n't,
  without, ...), a hypothetical or time shift (if, would, may, expected, previously, last,
  ...; also requires, needs, must, when, otherwise, criteria, conditions, eligible, allows,
  satisfies, qualifies), a hedge (roughly, about, approximately, around, nearly, almost,
  ballpark, circa, est., abt., apprx, rounded, rough, ish, loosely, broadly, `~`, `≈`, `±`,
  give or take, more or less, or so), a denial ("false", "untrue",
  "wrong"), "respectively", a "?", or a
  comparison word no binding used ("$812, or less than that", "under P6.1") anywhere in the
  reason makes every check of that reason ambiguous: it neither verifies nor contradicts.
- **Claims it cannot read.** Words the check does not read ("rare", "brand new") can make a
  reason false while its numbers are true. Declare them with `unreadable=[regex, ...]`: a
  match outside every `ValuePhrase` match makes the reason unverifiable; a phrase that reads
  the word ("rare category" -> `category_freq < 0.05`) checks it instead.
- **Booleans and conditions.** `<field> is true|false` (and `<field>: false`,
  `(<field> true)`) is checked against a boolean field; any other "true" or "false" is an
  unbound claim, so its reason is unverifiable; `ValuePhrase(holds=...)` conditions
  use the rule grammar and are never executed.

This is stricter than B5.13's checker, which matched an unbound number to a nearby field or
any field close in value, and the closed world makes it stricter still: it verifies short,
plain statements ("Amount is $812.40.", "Hour is 3.", "Risk score 0.83, card history 40.")
and leaves prose unverifiable. On fraud-review-bedrock's 10,225 recorded reasons (written
as prose, with rule ids such as "P7.1"), 8 are verified (0.08%; 11 when its rule ids are
declared in `ignore`), 375 contradicted (3.7%, every one by a condition phrase) and the
rest unverifiable; its mutation test (D-42, 160,000 false claims) verifies none.

`value_check_scorer` scores a case: its `outcome` is `contradicted` when any reason is,
`verified` when every reason is (at least one), else `unverifiable`, and its value is 1.0
only for `verified`. A run's mean of this task score is therefore the share of cases whose
every reason was verified. The per-reason figures sit in the details: `reasons` (index,
outcome, `n_checked` per reason), `n_reasons`, `counts` and `shares` (each outcome's count
and share of the case's reasons; `shares` is null with no reasons). A failed call scores 0.0
with no outcome.

To report the three shares across a run, pass the results (of `value_check` or
`value_check_scorer`) to `value_check_shares(results, alpha=0.05)`. It returns
`ValueCheckShares`: `n_reasons`, the counts `verified`, `contradicted`, `unverifiable`,
`nothing_checked` (the unverifiable reasons with no check at all) and `n_skipped` (results
without an outcome, such as failed calls), and `verified_share`, `contradicted_share` and
`unverifiable_share`, each a share of all reasons with an exact (Clopper-Pearson) 1 - alpha
interval, or null with no reasons. Cite them as counts and shares, for example "of 10,225
reasons, 8 verified (0.08%), 375 contradicted (3.7%), 9,842 unverifiable (96.3%)".

```python
from evalkit.scorers.evidence import value_check, value_check_shares  # internal until 0.4.0

results = [value_check(r, record, aliases=ALIASES, units=UNITS) for r in reasons]
shares = value_check_shares(results)
print(shares.contradicted, shares.n_reasons, shares.contradicted_share)
```

In `rule_compliance`, when the allow predicate reads a field (e.g. `action == "clear"`), a
structured `label` that says clear does not override an answer JSON whose `action` says hold:
the decision is read from the field the predicate names.


```python
from evalkit.scorers import Rule, RuleSet, rule_compliance_scorer

RULES = RuleSet(
    fields={"amount": "number", "merchant_first_seen": "bool", "risk_score": "number"},
    rules=(
        Rule(id="P7.2", kind="allow", when="amount < 50 and not merchant_first_seen"),
        Rule(id="P8.9", kind="never", when="risk_score >= 0.9"),
    ),
)
policy_check = rule_compliance_scorer(
    RULES, name="policy_check", cited_key="policy_clauses", allow='label == "clear"'
)  # ship it under the `evalkit.scorers` entry-point group, then list `policy_check` in a task
```

### `sql_match` shape equivalences (0.4.0, B5.15)

`sql_match` is strict by default. A SQL agent can return a right answer in a different shape;
name the shapes you accept, per call, and pass the question text:

```python
sql_match(predicted, gold, allow_extra_columns=True,
          question="What percentage of sales were new builds?",
          equivalences=("percent_scale", "pivot", "empty_check", "period_number"))
```

Every rule may only reshape a right answer, never make a wrong one right. A column a rule
converted must be compared with a gold column of its kind. Column names are read as whole
words (split at `_`, `-`, spaces and digits, any case). A word that must name a column is not
split at camelCase (`newBuildPct` is not a percent column), but an excluding word is
(`avgPrice` contains avg).

- `percent_scale`: the question asks for a percentage ("percent", "percentage", "per cent",
  "%"; not "share", "proportion" or "percentile") and never negates one ("not %", "not a
  percentage", "rather than a percent", "instead of a percentage"). A gold column has the
  word `pct`, `percent` or `percentage` in its name, or both `points` and `change` when the
  question asks for percentage points. `latency_percentile_95`, `avg_loyalty_points`,
  `pcnt` and `rate` do not count. A predicted column that fits such a column only once
  multiplied by 100 (0.0817 for 8.17) is scaled. That column alone is compared with a
  percent-unit tolerance, `abs` = max(`abs_tol`, 0.01), with your `rel_tol`. Every other
  column keeps your tolerance, so counts beside a percentage can stay exact. Note that the
  scaled column is compared at 0.01 percentage points even when `abs_tol` is smaller.
- `pivot`: a long gold (distinct text labels and numbers, two columns) matches one wide
  predicted row whose column names are the labels (case-insensitive; space, `-` and `_` alike).
  Any other predicted column must be a numeric 0 and look like a label the gold left out
  (`unknown = 0`). So it may not be a summary or metric word (`total`, `grand_total`, `sum`,
  `all`, `overall`, `count`, `n`, `avg`, `pct`, `share`, ...). It may not be one of the gold's
  column names. It may use only the kinds of character the gold's labels use, and must start
  with a digit when any label does: `unknown` beside year labels `2023`, `2024` (or beside
  `2023 Q1`, `2023 Q2`) is rejected, `2025` is accepted.
- `empty_check`: an empty gold matches one predicted row that is a zero count with only
  dates and nulls beside it (`earliest, latest, sales_in_1994 = 0`). Every number must be 0
  and sit in a count column. A count column is named with count, cnt, n, num, number, rows,
  records, sales, transactions, orders or items, and with none of avg, average, mean, median,
  min, max, sum, price, amount, value, revenue, pct, ratio, rate or share, and its first word
  is not year, q, quarter, month, day, id, rank, total or grand (such a column is a period,
  an id or a total, not a count). Every other cell must be null or an ISO date or timestamp.
  So `sales_in_1994 = 0` and `sales_in_requested_year = 0` count, while `total_sales = 0`,
  `year_count = 0`, `avg_price = 0.0`, `total = 0` and an error message do not. Name this
  rule only for questions whose answer is "no data".
- `period_number`: predicted periods in one calendar year can match gold numbers: month-start
  dates (`2023-03-01`), month labels (`2023-03`, `March 2023`, `Mar 2023`) and quarter labels
  (`2023 Q1`, `Q1 2023`, `2023-Q1`, `Q1`). They match month numbers (1–12) in a gold column
  named `month`, or quarter numbers (1–4) in a column named `quarter` or `qtr`, when the
  question names months or quarters. A quarter label never becomes a month number (a month label or month-start date at a quarter start, such as "April 2023", does read as that quarter, the same as the date 2023-04-01). empty_check refuses a total anywhere in a column name (`sales_total`, `grandtotal`). When the
  question names a year (19xx or 20xx), the periods must fall in a named year. A bare `Q1`
  has no year, so it is read only when the question names exactly one year (as that year);
  with no year or several named it stays wrong. Fiscal questions (`FY2024`, "fiscal") never
  convert. `Q5`, `2023-13` and week labels (`2023-W05`) are not periods. A count of 1–12 (or
  a `week_of_month`, `month_count`) is never read as a month.

The strict match runs first; only when it fails is each named reshaping of `predicted`
matched strictly in turn (same tolerance apart from the scaled percent column, same columns,
row count and `gold.ordered`). With equivalences named, `details` carries `correct_strict`
and the `equivalences` that matched (and `strict_reason` on a rescue), so report both
scores. With none named, the result is the same values and reasons as before 0.4.0 (failure details were made deterministic across hash seeds and regenerated once). The rules
start from agentic-analytics' (its OWNER D-81, EvalKit D-71). Where its D-91 narrowing and
EvalKit differ, EvalKit keeps the stricter rule.

## Writing a model adapter

An adapter subclasses `ModelAdapter` and implements `complete` (one request) plus
`submit_batch` and `collect_batch` (batch judging; raise `NotImplementedError` if the
provider has no batch API and run with `--live-judges`).

```python
from evalkit.adapters import ModelAdapter, RawCompletion, register


@register("my_provider")
class MyProvider(ModelAdapter):
    provider = "my_provider"

    async def complete(self, req):
        text = ...  # call your model with req.prompt, at most req.max_output_tokens
        return RawCompletion(
            model=req.model, model_snapshot=req.model_id, params=dict(req.params),
            raw_text=text, tokens_in=0, tokens_out=0, latency_ms=0, cost_usd=0.0,
        )

    async def submit_batch(self, reqs):
        raise NotImplementedError

    async def collect_batch(self, batch_id, reqs):
        raise NotImplementedError
```

Publish it under the `evalkit.adapters` entry-point group:

```toml
[project.entry-points."evalkit.adapters"]
my_provider = "my_package.adapters:MyProvider"
```

`evalkit.adapters.discover()` lists it and `get_adapter("my_provider")` builds it, for use in
your own pipelines. Built-in names (`anthropic`, `openai`, `openai_compatible`) cannot be
taken. Routing a model in `evalkit.yaml` to a plugin adapter is not supported yet; for an
HTTP model, an OpenAI-compatible endpoint is the supported route today. To evaluate your own
system (one that retrieves, keeps a conversation, or calls tools), write a system adapter.

## Evaluating your own system (0.2.0)

A model adapter sees only the rendered prompt. A **system adapter** sees the whole case: its
`id`, `question`, `context`, `reference_label` and `meta` (any per-case data from the
dataset, such as prior conversation turns), plus the request a model would have received.

```python
from evalkit.adapters import CompletionRequest, RawCompletion, SystemAdapter, register_system
from evalkit.schemas import Case


@register_system("my_assistant")
class MyAssistant(SystemAdapter):
    version = "1"  # part of the cache key: bump it when your system's answers change

    async def answer(self, case: Case, request: CompletionRequest) -> RawCompletion:
        reply = await my_system.ask(case.question, history=case.meta.get("prior_turns", []))
        return RawCompletion(
            model=request.model, model_snapshot=reply.model, params={},
            raw_text=reply.json,  # the task's answer JSON: label, answer, citations
            tokens_in=reply.tokens_in, tokens_out=reply.tokens_out,
            latency_ms=reply.latency_ms, cost_usd=reply.cost_usd,
        )

    def estimate_cost(self, case, request):
        return 0.01  # the most one call can cost; held against --max-cost while it runs
```

Route a name in `evalkit.yaml` to it, and list that name under `models`:

```yaml
models: [my-assistant, claude-sonnet-5-5]
systems: {my-assistant: my_assistant}
```

To ship it in a package, use the `evalkit.systems` entry-point group:

```toml
[project.entry-points."evalkit.systems"]
my_assistant = "my_package.system:MyAssistant"
```

The answer is validated, split into claims, judged and scored like a model's. The name needs
no price or API key, it is reported in the `other` family, and the cost estimate counts it
at $0; the cost you return is what is recorded. Answers are cached by the whole case and
`version`, so rerunning is free and a changed case or version is asked again. Raise a
transient error (`ConnectionError`, `TimeoutError`, or one with a 429 or 5xx `status_code`)
to record a failed call; any other exception stops the run. A system name cannot be a model
id a provider already serves, nor the extractor or a judge.
