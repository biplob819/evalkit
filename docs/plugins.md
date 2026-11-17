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
  Conditional texts may hold colons (`{flag?yes: really|no}`).
- **Checking a reason's numbers against the record**: `value_check`, coming in 0.4.0; see
  [value_check: coming in 0.4.0](#value_check-coming-in-040-d-74).

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

#### value_check: coming in 0.4.0 (D-74)

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
| `verified` | at least one check, every check is `ok`, and no time, window or constant was left unchecked | 1.0 |
| `contradicted` | at least one check `failed`: the record says otherwise (a false claim), even if other checks in the reason are ambiguous | 0.0 |
| `unverifiable` | no check failed, but at least one is `ambiguous` or `unchecked`, a time, window or constant was read but not checked, or nothing in the reason was checkable | 0.0 |

Each check carries `status`:

- `ok`: the claim holds.
- `failed`: false on a clear reading. A number or phrase whose field the record holds with
  another value ("Risk score of 0.3" on 0.39, "rare category" on `category_freq` 0.07 when
  rare is declared as `< 0.05`), or a plain number that no field equals ("It was 950").
- `ambiguous`: the checker is not sure of its reading. `ambiguous_negation`: a negation
  elsewhere in the sentence ("no doubt above $900") or after the claim ("Amount above $800
  is not true") leaves it unsettled. `unclear_field`: the clause names more than one field and
  the comparison holds for some but not all, or names a field only by one shared word ("card",
  "risk"). `unclear_subject`: a word after the compared number names something else ("more
  than 50 earlier transactions"). `unclear_comparison`: a comparison word in the clause is not
  directly before the number ("above what the policy allows, 812.40"). `unclear_time`: an hour
  field holds a whole hour and the minutes would decide it ("before 3:30 a.m." on hour 3).
- `unchecked`: the record cannot settle it: `missing_value:<field>`, `type_error:<field>`,
  or `no_field`, a comparison whose clause names no field and that no field is near
  ("It is below the 10x limit", "spend is under 50" without an alias for spend).

Ambiguous and unchecked checks are never verified and never counted as contradictions; nor
is a reason with a time, window or constant read but left unchecked (B5.13 verifier pass 6:
"Purchase at 7 p.m., amount $812.40" with no hour field named is unverifiable, never verified
on its amount alone).
`value` keeps its meaning "verified": 1.0 only for `verified`, so a false claim never scores
1.0 and neither does a claim the checker could not read. This changes one older reading: a
reason with nothing checkable (no number, no declared phrase, only times, ids or constants)
used to score 1.0; it now scores 0.0 as `unverifiable`, never as `contradicted`. Details
also hold `failed` (the text of each failed check), `unverified` (the text of each ambiguous
or unchecked check, then each unchecked time, window or constant), `checks`, `n_checked` and
`unchecked`.

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
interval, or null with no reasons. Cite them as counts and shares, for example "of 3,913
reasons, 2,941 verified (75.2%), 199 contradicted (5.1%), 773 unverifiable (19.8%)".

```python
from evalkit.scorers.evidence import value_check, value_check_shares  # internal until 0.4.0

results = [value_check(r, record, aliases=ALIASES, constants=CONSTANTS) for r in reasons]
shares = value_check_shares(results)
print(shares.contradicted, shares.n_reasons, shares.contradicted_share)
```

Comparisons in `value_check`: a number after a comparison word (above, over, exceeds, more/greater/higher than, at least, at or above, below, under, less/fewer/lower than, at most, up to, at or below, "... or equal to") is checked as that comparison; a negation directly before the word (not, no, never, cannot, n't: "not above", "doesn't exceed", "no more than") inverts it; any other negation in the same sentence ("no doubt above $900", "not only above", "without exceeding", double negatives) makes the check ambiguous (status `ambiguous`, reason `ambiguous_negation`), so its reason is unverifiable: a false claim never passes because of a negation, and some true ones cannot be verified (a documented limit), and a phrase right after the number ("$800 or less", "41 or more") states it too. "Any other negation in the same sentence" means anywhere earlier in the sentence, however far back. Which field a comparison talks about: the fields its clause names. A record key is named by its whole name as words, case-insensitive (`risk_score` by "risk score" or "risk_score"; `card_history_n` by "card history", a last token under three letters may be left out), and by the phrases you pass in `aliases={"amt_vs_usual": ["usual", "card's usual"]}`; one shared word ("card", "risk", "history") is not a mention, but the field it may mean is a candidate. A clause ends at a sentence end, `;`, or a contrast (", but", ", while", ", whereas", ", yet", ", though", ", although"); every mention before the number in its clause counts, and one after it unless a comma, "and", "which", "so" or another boundary lies between (a one-word mention after it only within three words). A comparison is verified only when it holds for every candidate field and contradicted only when it fails for every one; otherwise it is `unclear_field` (so "The risk score, unlike the amount, is above 0.9" is unverifiable, never verified on the amount). A word right after the compared number that is not a unit, a function word, a threshold word or a field ("more than 50 earlier transactions") makes it `unclear_subject`. So "risk score is below 0.9" passes on risk_score 0.39 even though 0.39 is far from 0.9, and "risk score below 0.9" fails on risk_score 0.95 even when another field is near 0.9. A multiple ("5x", "5X", "5 x", "5 times", "10 or more times") names only a field whose key marks a ratio (vs, ratio, multiple, times, x, fold, relative), never a plain amount named nearby. "...and the 10x limit" after "above the 5x signal" shares that comparison; ", which is at least 200" right after a number refers to that number's field. A comparison that names no field is unchecked as `no_field` and its reason unverifiable: it is never checked against a field that happens to be near the number (a documented limit: "It is below the 10x limit" and "$800 or more" with no field in their clause are never verified). "exceeds the threshold of 0.9", "below the extreme-spend limit of 10", "meets the threshold (10x)", "above USD 850" and "above both 800 and 900" (both bounds) are read as comparisons; any other comparison word in the clause that is not directly before the number makes it `unclear_comparison`, never an equality. A negation after the claim in its sentence makes it ambiguous when no clause boundary lies between ("Amount above $800 is not true"), when it follows the claim's comma directly ("812.40, not 900"), or when a word of truth follows it ("..., which is not true", "... the record does not show"); so do a denial word ("untrue", "wrong") and a next sentence that is only a denial ("A new merchant? Not at all."). A negation in a later clause with its own subject ("..., which is not a spend signal") and a hyphenated rule name after the claim ("the 10x never-auto-clear limit") deny nothing. Times of day ("10 p.m.", "6am", "10:30 pm", "22:00") are not free numbers: one is checked against an hour field (a key with the word hour, such as `hour` or `ts_hour`) named in its clause, or against the field of a `ValuePhrase` that matches it, and otherwise recorded in `unchecked` (never a failure, but its reason is unverifiable). A whole hour allows any minute of it: "3 a.m." equals hour 3, "3:45 a.m." or "before 3:30 a.m." cannot be settled by it (`unclear_time`). A window ("10 p.m. to 6 a.m.", "midnight-to-5 a.m.", "10 to 6 a.m.", read both ways when its first hour is bare) is checked against the hour field its clause names when the clause says how the hour sits against it (in, inside, within, during, between; outside, beyond; after; before; only one kind), else `unclear_time`; with no hour field named it is recorded in `unchecked` and its reason is unverifiable. `constants=[5, 10, 0.9]` declares policy thresholds: a number equal to one, with a threshold word next to it ("the 5x signal", "threshold of 0.9"), not compared and equal to no field its clause names, is recorded in `unchecked` as a constant, and its reason is unverifiable (a constant never makes a reason verified); a comparison ("below the 0.9 threshold") is always checked. Numbers after "rule", "clause" or "section" are ids, and ids glued to letters ("P7.1") are never numbers. `ignore=[regex, ...]` records the numbers inside matched spans as unchecked; it never hides a negation. A negation directly before a `holds` phrase ("not a first purchase at this merchant", "isn't at night") inverts the condition; any other negation earlier in the sentence makes it ambiguous (`ambiguous_negation`), as for comparisons (negations inside the phrase's own match, as in "never been used at this merchant", are its words). Details carry `unchecked` (text and kind: time, time_window, constant, id, ignored) next to `checks`, `failed` and `n_checked`. A percent compared with a field held as a fraction (|value| <= 1, e.g. `risk_score` 0.83) is read as a fraction, so "over 80%" passes and "at most 1%" fails. A field of the wrong type leaves its claim unchecked with reason `type_error:<field>`; `value_check` never raises on record contents.

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
  question names months or quarters. A quarter label never becomes a month number. When the
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
