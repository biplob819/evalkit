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
- **Structured evidence.** `render_evidence(record, template)` turns a record into evidence
  sentences for `FaithfulnessItem.evidence`, so `faithfulness()` judges each reason against
  the record (`"The amount is {amt_vs_usual} times the card's usual spend."`,
  `"{merchant_first_seen?First purchase at this merchant.|A known merchant.}"`).
  `value_check(reason, record, *, phrases=(), tolerance=0.1)` checks the reason's numbers
  against the record's fields, and each `ValuePhrase(pattern, field=...)` or
  `ValuePhrase(pattern, holds="hour < 5")` you declare ("9x usual spend", "at night").
  A comparison word before a number ("above 900", "at least 3") is checked as that
  comparison; a minus sign must match a negative value; a phrase over a field the record
  lacks fails with `missing_value:<field>`. Conditional texts may hold colons
  (`{flag?yes: really|no}`).
  `value_check_scorer(phrases=..., name=...)` checks each string in a JSON answer's
  `reasons` list.

Comparisons in `value_check`: a number after a comparison word (above, over, more/greater/higher than, at least, no less than, not below, below, under, less/fewer/lower than, at most, up to, no more than, not above) is checked as that comparison against the field it names, which is the first field within tolerance of the number. A percent compared with a field held as a fraction (|value| <= 1, e.g. `risk_score` 0.83) is read as a fraction, so "over 80%" passes and "at most 1%" fails. A field of the wrong type fails its claim with reason `type_error:<field>`; `value_check` never raises on record contents. In `rule_compliance`, when the allow predicate reads a field (e.g. `action == "clear"`), a structured `label` that says clear does not override an answer JSON whose `action` says hold: the decision is read from the field the predicate names.

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
