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
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 50 --dry-run
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 50
```

`--n` draws a seeded sample stratified by label, so reruns with the same seed use the same
cases. To use every case, set `--n` to the number of cases in the file.
