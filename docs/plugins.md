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

### Worked example: Gemma through llama-server (B5.17)

[Gemma Grounded Health](https://github.com/biplob819/gemma-grounded-health) (GGH) fine-tuned
Gemma 4 E2B to answer PubMedQA questions with citations, quantized it to a Q4_K_M GGUF, and
scored it with EvalKit 0.4.0 on a laptop: llama.cpp's `llama-server` served each GGUF on its own
port, and EvalKit reached it through `endpoints`, which routes a model to the `openai_compatible`
adapter. Its v2 main run (`r-715fc213a8a5`, 1,000 cases, three GGUFs) is published at
[/gemma/](https://biplob819.github.io/evalkit/gemma/). The model is
[biplob123/gemma-4-e2b-grounded-health](https://huggingface.co/biplob123/gemma-4-e2b-grounded-health)
and the GGUFs are in
[biplob123/gemma-4-e2b-grounded-health-GGUF](https://huggingface.co/biplob123/gemma-4-e2b-grounded-health-GGUF)
(`gemma-4-e2b-grounded-health-Q4_K_M.gguf` is the file GGH scored as `gemma-e2b-grounded-v2-q4`).

**1. Serve the GGUF.** These are the flags GGH's `ggh serve` passed (its `src/ggh/serve/llama.py`,
llama.cpp release `b12421`, Apple Metal); one server per model, each on its own port:

```bash
llama-server --model gemma-4-e2b-grounded-health-Q4_K_M.gguf \
  --host 127.0.0.1 --port 8083 --n-gpu-layers 999 --parallel 1 --seed 7 --threads 8 \
  --ctx-size 8192 --temp 0 --top-k 1 --jinja --reasoning off --reasoning-budget 0 --no-webui
```

**2. Point EvalKit at it.** `examples/llama-server/evalkit.yaml` is GGH's `configs/evalkit_v2.yaml`
cut to the v2 model (GGH also listed its base and v1 GGUFs, on ports 8080 and 8081):

```yaml
# A GGUF served by llama-server, scored by EvalKit (docs/plugins.md, "Gemma through llama-server").
# Gemma Grounded Health's v2 main run (r-715fc213a8a5), cut to its v2 model: its
# configs/evalkit_v2.yaml listed base and v1 too, each on its own port (8080, 8081).
# Anything under `endpoints` goes to the `openai_compatible` adapter; no API key is sent.
models: [gemma-e2b-grounded-v2-q4]
endpoints:
  gemma-e2b-grounded-v2-q4: http://127.0.0.1:8083/v1
judges: [gpt-6.1-sol]            # one judge: self-preference is not measured
extractor: gpt-6-luna
structured_output: true          # llama-server turns the JSON schema into a grammar
max_output_tokens: 2048
concurrency: {openai: 8, openai_compatible: 1}   # one llama-server slot: one request at a time
seed: 7
default_effort: medium           # not sent to the endpoint unless the model is under `efforts`
judge_effort: medium
extractor_effort: medium
max_cost_usd: 16                 # judge and extractor calls; the local model costs $0
```

```bash
evalkit run examples/llama-server/evalkit.yaml --dry-run   # the estimate; no API calls
```

The endpoint model has no price in `prices.yaml`, so it is costed at $0 with a warning; the
judge and extractor are paid OpenAI calls and need `OPENAI_API_KEY`.

**Gotchas GGH hit.**

- **Turn Gemma 4's thinking channel off.** EvalKit sends chat completions with a JSON schema.
  With `--reasoning off` alone, the b12421 Gemma 4 chat grammar still admitted a thought channel,
  and the fine-tuned model opened it and answered after a long trace (on 16 smoke prompts Q4_K_M
  agreed with bf16 on 8). `--reasoning-budget 0` closes the channel at once (15 of 16 agreed);
  the base model never opened it (GGH D-41). Through Ollama, Gemma 4 thinks first unless run
  with `--think=false` (`ollama run --think=false hf.co/biplob123/gemma-4-e2b-grounded-health-GGUF:Q4_K_M`),
  and Ollama samples at its default temperature, so its answers can differ from llama-server's
  greedy ones.
- **Greedy decoding is the server's job.** EvalKit sends no sampling parameters, and sends an
  endpoint model no reasoning effort unless the model is listed under `efforts`, so the server's
  defaults decide: `--temp 0 --top-k 1 --seed 7` make every request greedy and repeatable.
- **One slot means requests queue.** `--parallel 1` gives llama-server one slot; extra
  concurrent requests wait in its queue and their latency includes the wait. Set
  `concurrency: {openai_compatible: 1}` so EvalKit sends one request at a time and the latency
  it records is the model's. The limit is per provider, so every endpoint model in the run
  shares it, even on separate servers.
- **Cache keys name the model, not the server.** An answer's cache key is the case, prompt,
  model name and request settings; the endpoint URL and the GGUF behind it are not part of it.
  Give every GGUF its own model name (GGH: `gemma-e2b-base-q4`, `gemma-e2b-grounded-q4`,
  `gemma-e2b-grounded-v2-q4`): swapping the file behind a name returns the old answers from the
  cache. The other side of this is useful: GGH re-scored base and v1 in its v2 run from the cache
  with their servers stopped, so a missing answer failed loudly instead of being regenerated.

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

### Agent and decision scorers (0.3.0; value_check 0.4.0)

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

`value_check` verifies plain "field is value" statements only, and every other number in a reason makes it unverifiable. To get coverage, ask your model to end each reason with a short facts line built from field names, for example: "Facts: Amount is 812.40. Hour is 3. Risk score is 0.83." The check verifies such a line reliably. Prose is not read and does not need to be, but a statement with prose before it or after it is not read either: put the facts line last, after the prose on a new line that starts with a facts header ("A large late-night purchase.\nFacts: Amount is 812.40. Hour is 3."), or in its own reason string. The grammar it reads is stated exactly in [The facts grammar](#value_check-the-facts-grammar-verify516c).

#### value_check: the facts grammar (verify516c)

A reason is read as a run of statements. Each statement binds a value to a field, and a
statement binds only where the grammar says it may start. Everything else is unbound, so a
reason holding it is unverifiable (never verified, never contradicted).

- **Statements.** `<Field> is <value>` and `<Field>: <value>` (key: value). `was`, `equals`
  and `=` read like `is`. A comparison may stand in place of the connector or after `is` /
  `was`: `<Field> [is|was] above|over|below|under|exceeds|more than|less than|at least|at
  most|>|<|>=|<=|≥|≤ <value>`. A boolean field takes `true` / `false` the same way ("Flag
  is true", "Flag: false"). Nothing else connects a field to its value: "Amount 812.40",
  "Amount of 812.40", "Risk score (0.83)" and "40 card history" bind nothing.
- **Field names.** The record key or an alias you declare, case-insensitive, with spaces and
  underscores interchangeable (`risk_score` = "Risk score" = "RISK_SCORE"). The longest name
  wins: with both `amount` and `amount_usd` in the record, "Amount usd is 1300.00" is about
  `amount_usd`. Before the field name only nothing or "The" / "the" may stand: never "Its",
  "Their", "A", "This", a possessive or any other word.
- **Left boundary.** A statement binds only when it starts at (1) the start of the reason;
  (2) right after a facts header on its own, at the start of the reason or of a line:
  `Facts:`, `Facts -`, `Facts –`, `Facts —`, `Key facts:`, `Key facts -`, `Key facts –` or
  `Key facts —` (this closed list, any case, also in bold as `**Facts:**`; the statements may
  follow on the same line or the next; a header starts a new run, whatever the prose before
  it says, so prose that changes the subject, such as "For the previous card:" followed by
  "Facts: Amount is 812.40.", is not read and the facts are checked against the record as
  given; keep facts about the record only, and never put a facts header under prose that
  qualifies it); or (3) right after the terminator of the statement before it, when that statement
  itself bound (a chain). Any other text before a statement (an unknown word or abbreviation
  such as "Prev." or "Merch.", a heading such as "Merchant:" or "Refund facts:", a pronoun, a
  prose sentence) breaks the chain: that statement and every later one in its run are unbound.
- **Code fences.** A reason wrapped whole in a code fence is read as its contents; a fence's
  info string (```` ```refund ````) is dropped and not read, so it cannot qualify the facts.
- **Terminators.** A statement ends at the end of the reason, a line break (each line is one
  statement; a full stop at the line's end is optional), `. `, `;`, or `,` / `and` / `, and`
  followed by another statement that binds. A value followed by anything else is unbound:
  "Amount is 812.40, which is high", "Risk score is 0.83 (high)", "Hour is 3 a.m. UTC".
- **Right boundary (verify516d).** Everything after a statement's terminator must be more
  statements of its run (each one bound), a new facts header on its own line (starting a new
  run), or the end of the reason (whitespace only). Any other text after it, on the same line
  or a later one, unbinds every statement of its run, because a trailing fragment can change
  the subject, unit or zone: "Amount is 812.40\nin euros", "Hour is 3\nUTC", "Risk score is
  0.83; of the merchant", "Amount is 812.40. In cents.", "Facts: Amount is 812.40. Hour is
  3.\nThese are the refund figures."
- **Wrappers.** When the whole reason, or a whole line, is wrapped in quotes (`"..."`,
  `“...”`), backticks, bold (`**...**`, `__...__`) or a code fence, or a line starts with a
  marker (`> `, `- `, `* `, `• `, `1. `, `1) `), the wrapper is removed first; the marker's
  number is not a claim. A partial wrapper stays and its statement is unbound ("**Amount** is
  812.40", "Amount is `812.40`").
- **Values and units.** A number with an optional sign, thousands commas and the field's
  declared unit (see Units below). A currency mark (`$`, `US$`, `€`, `£`, `USD`, `EUR`, `GBP`,
  dollars, euros, pounds) must name the currency the field declares: `currency` and `usd` are
  the US dollar, `eur` the euro, `gbp` the pound sterling; on a field without a currency unit,
  or naming another currency ("€812.40" on a dollar field), the value is unbound. A time zone
  or any other qualifier after the value ("3 a.m. UTC", "15:00 local") leaves it unbound.

Facts and prose: put facts last, or in their own reason string. Prose before the facts is
allowed only when the facts start on a new line with a facts header ("A large purchase.\nFacts:
Amount is 812.40."); prose before a statement without a header breaks the chain. Prose after
the facts is not allowed: "Amount is 812.40. A large purchase." is unverifiable, since the
trailing text could re-attribute the statement. A number or quantity word in the prose is
still unbound (closed world, below), and a negation or hedge anywhere still voids the reason.

<!-- value_check grammar examples: tests/scorers/test_value_check.py runs this block -->
```python
from evalkit.scorers import value_check  # public since 0.4.0

record = {"amount": 812.4, "amount_usd": 1300.0, "hour": 3, "prev_hour": 14,
          "risk_score": 0.83, "flag": True}
units = {"amount": "currency", "amount_usd": "currency", "hour": "hour",
         "prev_hour": "hour", "risk_score": "fraction"}

verified = [
    "Facts: Amount is 812.40. Hour is 3.",
    "Facts: Amount is $812.40. Hour is 3. Risk score is 0.83.",
    "Facts:\nAmount is $812.40\nHour is 3",
    "Key facts: Amount is $812.40, hour is 3 and the risk score is 83%.",
    "A large late-night purchase at a new merchant.\nFacts: Amount is 812.40. Hour is 3.",
    "Facts: Amount is 812.40.\nKey facts: Hour is 3.",  # a header starts a new run
    "amount: 812.40\nhour: 3\nrisk_score: 0.83\nflag: true",
    "Amount is 812.40 USD; hour was 3 a.m.; risk score is above 0.8.",
    "Amount usd is $1,300.00.",
    '"Amount is 812.40. Hour is 3."',
    "```\nAmount is 812.40.\nHour is 3.\n```",
    "1. Amount is 812.40.\n2. Hour is 3.",
    "- **Amount is 812.40.**\n- Hour is 3.",
]
unverifiable = [
    "A large purchase. Amount is 812.40.",       # prose before the facts, no header
    "Amount is 812.40. A large purchase.",       # prose after the facts: put facts last
    "Facts: Amount is 812.40. Hour is 3.\nThese are the refund figures.",
    "Amount is 812.40\nin euros",                # a fragment after the terminator
    "Hour is 3\nUTC",
    "Prev. hour is 3.",                          # unknown abbreviation before the field
    "The merchant is new. Its risk score is 0.83.",
    "Merchant:\nRisk score is 0.83.",            # not a facts header
    "Amount 812.40.",                            # no connector
    "Amount is €812.40.",                        # not the declared currency
    "Hour is 3 a.m. UTC.",                       # a qualifier after the value
    "**Amount** is 812.40.",                     # a partial wrapper
    "Amount is 812.40, which is high.",
]
contradicted = [
    "Facts: Amount is 900.00. Hour is 3.",
    "Amount usd is 812.40.",                     # the longest name: amount_usd is 1300.00
    "Prev hour is 3.",                           # prev_hour is 14
]
for want, reasons in (("verified", verified), ("unverifiable", unverifiable),
                      ("contradicted", contradicted)):
    for reason in reasons:
        assert value_check(reason, record, units=units).details["outcome"] == want, reason
```


**Public since 0.4.0.** `value_check`, `value_check_scorer`, `value_check_shares`,
`ValuePhrase` and `ValueCheckShares` are exported from `evalkit.scorers` (B5.16, D-77). 0.3.0
withheld them (OWNER D-74): its checker failed seven independent verification passes, because
when it could not tell which field a number was about it could match the number against any
field close in value, so some false claims came back `verified`. The 0.4.0 check is the strict
redesign described here: `verified` only when every number is bound by a framed "field is
value" statement; every other reading is `unverifiable`.

#### value_check: three outcomes (D-72)

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
  `malformed`, `constant`, `number_word`, `non_ascii_number`, `id`, `unread_number`,
  `double_bound` or `unframed` (a statement outside the facts grammar; a condition phrase
  that is not a whole statement is `unbound` with `unframed` too).
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
- **Statements (the facts grammar, verify516c).** A number binds to a field only in a
  statement of [the facts grammar](#value_check-the-facts-grammar-verify516c): `<field>
  is|was|equals|=|: <n>` or `<field> [is|was] <cmp> <n>`, starting at the start of the
  reason, after a facts header on its own, or after the terminator of a statement that
  itself bound, with only "the" before the field, and ending at a terminator. A
  `ValuePhrase` match is a statement too: the one number inside a `ValuePhrase(field=...)`
  match is bound to its field (a comparison word right before it is read), and a
  `ValuePhrase(holds=...)` match is checked, only when the match itself starts and ends a
  statement ("9.1x usual." and "At night." bind; "Spend is 9.1x usual" and "Purchase at
  night" do not: declare the subject in the pattern if you need it). A phrase's number binds
  nothing when another field is named right before or after it or anywhere in its clause:
  "Risk score is 9.1x usual" binds nothing to `amt_vs_usual`. Fields joined by and / or /
  `,` / `/` / `&` ("Risk score and hour: 3"), a field on both sides of a number, a field
  named for two numbers, and a number bound to two fields bind nothing. "41 or more" is not
  read as a comparison: the reason is unverifiable. A statement that breaks the grammar is
  unbound (`unframed`): "For the refund, amount is $812.40", "The card has 40 earlier
  purchases", "Flag is true (for the previous card)", and every hedge spelling after a
  number ("900-ish", ", est.", "(rounded)", "±5", "+/- 5", ", give-or-take").
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
  0.83), `percent` (field 0 to 100), `multiple` ("9x", "9 times", "9-fold"), `currency` or
  `usd` (US dollars: "$812.40", "US$812.40", "812.40 USD", "812.40 dollars"), `eur` ("€812.40",
  "812.40 EUR", "euros"), `gbp` ("£812.40", "812.40 GBP", "pounds"), `hour` (field 0 to 23;
  "3 a.m.", "3pm", "15:00"; other minutes are unbound). Without a declared unit only a bare
  number binds: "risk score is 83%" is unverifiable. A currency mark must name the field's
  declared currency, also inside a phrase; a bare number binds to a currency field. On a
  `fraction` field a bare number above 1 ("category freq is 3.8") is unchecked. Scales and
  suffixes ("5k", "thousand", "cents", "3rd", "1e9") never bind. A phrase-bound number may
  carry a multiple mark.
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
- **Booleans and conditions.** `<field> is true|false` (and `<field>: false`, `<field> =
  true`) is checked against a boolean field; any other "true" or "false" is an unbound
  claim, so its reason is unverifiable; `ValuePhrase(holds=...)` conditions use the rule
  grammar and are never executed.

This is stricter than B5.13's checker, which matched an unbound number to a nearby field or
any field close in value, and the closed world and the facts grammar make it stricter
still: it verifies facts lines ("Facts: Amount is $812.40. Hour is 3.") and leaves prose
unverifiable. On fraud-review-bedrock's 10,225 recorded reasons (written as prose, with
rule ids such as "P7.1"), 4 are verified (0.04%), none contradicted and the rest
unverifiable; its mutation test (D-42, 160,000 false claims) verifies none. Facts lines in
the documented forms verify: 81,000 of 81,000 built from verify516c's generator records.

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
reasons, 4 verified (0.04%), 0 contradicted, 10,221 unverifiable (99.96%)".

```python
from evalkit.scorers import value_check, value_check_shares  # public since 0.4.0

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
