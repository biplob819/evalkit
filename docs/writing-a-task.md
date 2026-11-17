# Writing a task

A task is a YAML file that names your data, the labels an answer may take, the prompt, and
the scorers. `evalkit init` writes a working example in `tasks/`.

## The data

Each case has an `id`, a `question`, a `context` (the source the answer must come from) and,
optionally, a `label` (the correct answer). Cases without a label still get groundedness and
citation scores, just not accuracy.

### JSONL

One JSON object per line. `context` is a list of sentences; the model cites them by number,
starting at 1.

```json
{"id": "q1", "question": "Does the policy cover floods?", "context": ["The policy covers fire.", "Floods are excluded."], "label": "no"}
```

An optional `meta` object carries any other data about the case (0.2.0). EvalKit does not
read it itself; it reaches system adapters, task scorers and slices (see below and
[plugins.md](plugins.md)):

```json
{"id": "q2", "question": "Is theft covered?", "context": ["Theft is covered."], "label": "yes", "meta": {"topic": "theft", "split": "heldout"}}
```

### CSV (a spreadsheet export)

Columns `id`, `question`, `context` and optionally `label`. The `context` cell holds the
source text; EvalKit splits it into sentences, and every line break inside the cell also ends
a sentence. Save as UTF-8 CSV from Excel, Numbers or Google Sheets.

```text
id,question,context,label
q1,Does the policy cover floods?,"The policy covers fire. Floods are excluded.",no
q2,Is theft covered?,"Theft is covered up to $5,000.",yes
```

Rules for both formats: ids are unique and contain no `:`; every label is one of the task's
labels (case and surrounding spaces do not matter); every case has at least one sentence of
context; the file is UTF-8 (in Excel, Save As > CSV UTF-8). CSV column names may be in any
order and case, and other columns are ignored. A problem is reported with the file name and
the line its row starts on, before anything is sent to a model.

## The task file

```yaml
name: my_task
dataset: my_task.csv          # or my_task.jsonl; relative to this YAML file
split: all
labels: ["yes", "no", "maybe"]
output_schema: answer_v1
scorers: [label_correct, citation_valid, groundedness, coverage, cost, latency]
thresholds:
  - {metric: headline, op: ">=", value: 0.6}
prompt_template: |
  Answer the question using only the numbered sentences below.

  Question: {question}

  Sentences:
  {numbered_context}

  Reply with JSON only, in this shape:
  {{"label": one of {labels}, "answer": "two to four sentences", "citations": [sentence numbers]}}

  Cite the sentence numbers (1 to {n_sentences}) that support your answer.
```

The template can use `{question}`, `{numbered_context}` (the sentences as `[1] ...`),
`{labels}` and `{n_sentences}`. Literal braces in the JSON example are doubled (`{{`, `}}`).
Keep the reply shape: the answer, its label and the cited sentence numbers are what the
scorers read.

## Slices (0.2.0)

`slices` breaks the summary and the report down by a case field: `reference_label`, `source`
or a key of `meta`. Each slice gets its own figures per model (accuracy, headline and
groundedness per judge, citation validity, coverage, task scores, cost and latency), with
95% bootstrap intervals drawn over that slice's cases only.

```yaml
slices: [topic, split]
```

Values must be strings, integers or booleans; a case without the field is counted under
`(none)`. Small slices have wide intervals: read the interval, not just the point.

## Task scorers (0.2.0)

Any name in `scorers` beyond `label_correct`, `citation_valid`, `groundedness`, `coverage`,
`cost`, `latency` and `citation_support` is a scorer you register yourself; it runs on every
answer and its mean appears in the summary and the report. See [plugins.md](plugins.md).

## Citation support (0.3.0)

`citation_valid` only checks that cited sentence numbers exist, and a claim verdict judges
the cited sentences as a set. To see whether each cited sentence actually supports the claim
that cites it, and what part of the source it is, add `citation_support`:

```yaml
scorers: [label_correct, citation_valid, groundedness, coverage, cost, latency, citation_support]
```

After claim judging, each judge reads every claim that cites existing sentences once more,
with each cited sentence shown on its own (blinded and redacted like claim judging), and says
for every sentence whether it supports the claim and which role it plays: `aim` (the study's
question, objective or hypothesis), `background`, `method`, `result` or `conclusion`. This is
its own prompt (`CITATION_PROMPT_HASH`) and cache: claim verdicts, their prompt and their
cache keys do not change, so adding the scorer to a task you already ran reuses the cached
answers, claims and verdicts and pays only for the citation calls.

**Cost.** One more judge call per claim per judge, about the price of a claim verdict with a
slightly longer reply. Batch by default, live with `--live-judges`; `--dry-run` shows the
calls as `citation_judge` lines before anything is spent.

**Metrics**, per model and judge, with 95% bootstrap intervals over cases, in `summary.json`,
the report's model table and slices:

- **Citation precision**: supporting cited sentences over all cited sentences, summed over
  answers with at least one judged citation. A sentence supports if it supports at least one
  of the answer's claims.
- **Aim-citation rate**: the share of those answers that cite a sentence whose role is `aim`
  and that supports none of the answer's claims, such as an answer resting on the study's
  question instead of its result. A sentence's role is the most common one the judge gave it
  in that answer (ties go to `aim`, then `background`, `method`, `conclusion`, `result`).

Records keep each judge's verdicts in `citation_support`. While citation batches are still
open the run is "judging pending" (exit 4) and both metrics are absent; rerun the same
command to collect them.

## Thresholds

A model passes only if every threshold holds; `evalkit run` exits with code 1 when any model
fails one, which is how a CI job fails on a regression. Thresholds in `evalkit.yaml` take
precedence over the task's: the config written by `evalkit init` sets one, so edit it there,
or delete the `thresholds` block from `evalkit.yaml` to use the task file's. Metrics: `headline` (correct and
every claim supported), `groundedness`, `accuracy`, `citation_validity`, `coverage`,
`cost_per_100_usd`, `cost_per_correct_usd`, `latency_p50_ms` and `latency_p95_ms`.
Operators: `>=` and `<=`.

## Run it

```bash
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3 --dry-run
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3
```

`--n` is how many cases to evaluate, at most the number in the file (the sample task has
three; raise it for your own data). It draws a seeded sample stratified by label, so reruns
with the same seed use the same cases.
