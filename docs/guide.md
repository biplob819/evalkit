# User guide: evaluate your own task, end to end

This guide takes one task of your own from an empty folder to a gated, tracked evaluation:
write the cases, configure the models and judges, quote the cost, run, read the report,
calibrate the judges against your own labels, gate releases in CI, compare runs, keep a run
history and check for drift. Each step gives the exact commands and links to the reference
page that covers every option.

Only step 6 calls a model API, so only that step costs money. Steps 1 to 5 and 9 to 12 run
offline, need no keys and spend nothing. Step 8 needs about an hour or two of your time.

| Step | Command | Reference |
|---|---|---|
| 1. Install and look first | `pip install`, `evalkit report --example` | [quickstart](quickstart.md) |
| 2. Scaffold a project | `evalkit init` | [quickstart](quickstart.md#3-scaffold-a-project) |
| 3. Write your task | your JSONL or CSV, and a task YAML | [writing a task](writing-a-task.md) |
| 4. Configure models and judges | `evalkit.yaml` | [cost](cost.md), [plugins](plugins.md#models-without-a-plugin) |
| 5. Quote the cost | `evalkit run` with `--dry-run` | [cost](cost.md) |
| 6. Run with your keys | `evalkit doctor`, `evalkit run` | [cost](cost.md#exit-codes) |
| 7. Read the report | `report.html`, `summary.json` | [results format](versioning.md#results-format) |
| 8. Calibrate with hand labels | `evalkit calibrate` | [calibrate your task](calibrate-your-task.md) |
| 9. Gate in CI | thresholds, `evalkit gate` | [gates](gates.md), [CI example](../examples/github-action/evalkit.yml) |
| 10. Compare runs | `evalkit compare` | [compare](compare.md) |
| 11. Track with the ledger | `evalkit history`, `evalkit spend` | [ledger](ledger.md) |
| 12. Check drift | `evalkit drift` | [drift](drift.md) |

## 1. Install, and look before you spend

Python 3.11 to 3.13 on Linux, macOS or Windows.

```bash
pip install 'evalkit-harness[anthropic,openai]'
evalkit report --example
```

The package is `evalkit-harness` on PyPI; the command and the import are both `evalkit`. The
extras install the Anthropic and OpenAI SDKs; keep only the one you use. `report --example`
writes `evalkit-example/report.html` from a bundled run of scripted stand-in models, with no
keys and no network, so you can see what a report holds before you write anything.

## 2. Scaffold a project

```bash
evalkit init
```

`init` writes `evalkit.yaml` (models, judges, cost cap, thresholds), `.env.example`, a
`.gitignore` and a sample task: `tasks/my_task.yaml` and `tasks/my_task.jsonl` with three
cases. It never overwrites a file that exists.

## 3. Write your task

A case is a question, the source the answer must come from (its context, as sentences the
model cites by number), and optionally the correct label. Replace the three sample cases in
`tasks/my_task.jsonl` with yours, one JSON object per line:

```json
{"id": "q1", "question": "Does the policy cover floods?", "context": ["The policy covers fire.", "Floods are excluded."], "label": "no"}
{"id": "q2", "question": "Is theft covered?", "context": ["Theft is covered up to $5,000."], "label": "yes", "meta": {"topic": "theft"}}
```

A spreadsheet works too: save it as UTF-8 CSV with columns `id`, `question`, `context` and
`label`, and point the task's `dataset` at it. Then edit `tasks/my_task.yaml`: the labels an
answer may take, the prompt, the scorers and, if you want a breakdown, slices by a `meta`
field.

```yaml
name: my-task
dataset: my_task.jsonl          # or my_task.csv; relative to this file
split: local
labels: ["yes", "no", "maybe"]
output_schema: answer_v1
scorers: [label_correct, citation_valid, groundedness, coverage, cost, latency]
slices: [topic]                 # optional: figures per value of meta.topic
```

Keep the prompt's reply shape (a label, an answer and the cited sentence numbers): that is
what the scorers read. Problems in the data (a duplicate id, an unknown label, a case with no
context) are reported with the file and line before anything is sent to a model.

More: [writing a task](writing-a-task.md) (the prompt template, CSV rules, slices, task
scorers and per-sentence citation support), and [scorers and adapters](plugins.md) for your
own scorers, including `sql_match` with opt-in shape equivalences for SQL agents and
`value_check`, which verifies "field is value" facts lines in a model's reasons.

## 4. Configure the models and judges

`evalkit.yaml` names the models under test, the judges that check every claim, the claim
extractor, the cost cap and the thresholds a model must meet:

```yaml
models: [claude-sonnet-5-5, claude-haiku-5-5, gpt-6.1-sol, gpt-6-luna]
judges: [claude-opus-5-5, gpt-6.1-sol]   # two families; one is enough to start
extractor: claude-haiku-5-5
seed: 7
max_cost_usd: 20                         # total for a run folder, across resumes
thresholds:
  - {metric: headline, op: ">=", value: 0.6}
```

- **One provider is enough.** Keep only that provider's models, one judge and an extractor
  from it (for OpenAI only, for example `extractor: gpt-6-luna`).
- **Two judges from different families** are what let the report show self-preference (a
  judge going easy on its own family) and agreement between judges.
- **Local or self-hosted models** that speak the OpenAI API go under `endpoints:`; any other
  system plugs in as an adapter ([plugins](plugins.md#writing-a-model-adapter)).
- **Thresholds** use `headline` (correct and every claim supported), `groundedness`,
  `accuracy`, `citation_validity`, `coverage`, `cost_per_100_usd`, `cost_per_correct_usd`,
  `latency_p50_ms` or `latency_p95_ms`, with `>=` or `<=`. Thresholds in `evalkit.yaml` take
  precedence over the task file's.

## 5. Quote the cost before any call

```bash
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3 --dry-run
```

![A dry-run cost quote: calls and dollars per step and model, and the estimated total, with no API calls](assets/screens/dry-run.png)

The screenshot is the quote for the 200-case PubMedQA benchmark ($14.13); yours lists the
same lines for your models and cases. The dry run prints the calls and dollars per step and model, the claims-per-answer assumption
behind the judge lines, and the total, and makes no API call. `--n` is how many cases to
evaluate (a seeded sample stratified by label, at most the number in your file): raise it for
your own data. Judging dominates the bill; if your answers are longer than PubMedQA's, measure
claims per answer on a small run and pass `--claims-per-answer` ([cost](cost.md#claims-per-answer)).

## 6. Run with your keys

Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` and `OPENAI_API_KEY` (only the
providers you use). `.env` is git-ignored, and EvalKit never prints key values.

```bash
evalkit doctor --offline
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3
```

`doctor` names anything that would stop a run (Python, the SDKs, which keys are set, the age
of the price table); drop `--offline` to also check that the provider APIs answer. The run
answers each case with every model, splits each answer into claims, has every judge check
every claim against the sentences it cites, and writes `runs/<run id>/report.html` and
`summary.json`.

- **Every call is cached** by its exact inputs: a run stopped by the cap, a crash or Ctrl-C
  resumes for free with the same command.
- **Judges use the batch APIs at half price.** If a batch is still open when `--wait` minutes
  pass, the command exits 4; rerun it to collect at no extra cost. `--live-judges` judges at
  once at full price.
- **The cost cap** (`max_cost_usd`, or `--max-cost`) is checked before each call; at the cap
  the run stops with exit 3 rather than overspend.

| Exit | Meaning |
|---|---|
| 0 | finished, every threshold passed |
| 1 | finished, a threshold failed |
| 2 | configuration or runtime error |
| 3 | stopped at the cost cap; finished work is cached, so rerun to resume (after raising the cap) |
| 4 | judging pending; rerun the same command to collect at no extra cost |
| 130 | interrupted (Ctrl-C); finished work is cached, so rerun to resume |

## 7. Read the report

```bash
evalkit report RUN_DIR
```

`report.html` is one self-contained page (re-render it any time with `evalkit report`, where
RUN_DIR is the run folder). Read it top down:

1. **The run header** says which judges ran, whether judging is complete, and the calibration
   status. Until step 8 it carries the stamp "judge not calibrated for this task".
2. **The headline chart** plots each model's *correct and fully grounded* rate, per judge,
   with its 95% interval, against cost per 100 cases. A gap between two models counts only if
   it survives a Holm correction under the pairwise comparisons below the chart.

   ![The headline chart: correct and fully grounded rate per model and judge, with 95% intervals, against cost per 100 cases](assets/screens/report-scatter.png)

3. **The models table**: accuracy, groundedness per judge, citation validity, coverage, cost,
   latency and the efficiency block (tokens, cache hits, cost per correct answer), each with
   its interval; slices break the same figures down by your `meta` fields.
4. **Self-preference** shows whether a judge rated its own family's answers higher.
5. **The failure explorer** lists every answer that was not correct and fully grounded for at
   least one judge, with its claims, each judge's verdict and the sentences it cited. Filter
   by model and by judge. This is where you find out whether a failure is the model's, the
   judge's or the data's.

   ![The failure explorer: one failed answer with its claims, each judge's verdict and the cited sentences](assets/screens/failure-explorer.png)

`summary.json` holds every figure in the report in a stable, versioned format
([results format](versioning.md#results-format)); `records.jsonl` holds every answer, claim
and verdict.

## 8. Calibrate the judges with your own labels

Judge numbers mean little until someone has checked the judges. EvalKit compares each judge
with your labels on claims it never saw you tune on, and only then drops the "not calibrated"
stamp.

```bash
evalkit calibrate export RUN_DIR
evalkit calibrate label RUN_DIR
evalkit calibrate label RUN_DIR --relabel
evalkit calibrate score RUN_DIR
```

1. **Export** needs a run with at least 100 answers that made claims (for example 25 or more
   cases with four models). It picks 100 answers stratified by model and label, splits them
   50 tuning and 50 held-out, and writes a blind sheet: opaque ids, claims and cited
   sentences, no model names and no judge verdicts.
2. **Label** opens a page on `127.0.0.1` only. For each claim, ask whether the cited sentences
   alone establish it: <kbd>S</kbd> supported, <kbd>U</kbd> unsupported, <kbd>E</kbd> the
   claim misstates the answer. Every label saves at once, so you can stop and resume.
3. **Relabel** 20 answers blind, on another day if you can: your agreement with yourself is
   reported too, because no judge can be expected to agree with a labeller who disagrees with
   themselves.
4. **Score once.** Each judge gets Cohen's kappa against you on the held-out half, with a 95%
   interval over whole answers. The calibration is tied to the judge rubric's hash: change the
   rubric and the stamp returns until you label a fresh held-out set, and scoring the same
   held-out set twice is refused.

Your own judged criterion (an integrity probe, a policy check) calibrates the same way with
`--criterion`, on whole items instead of claims:
[calibrate any criterion](calibrate-your-task.md#calibrate-any-criterion).

## 9. Gate releases in CI

The thresholds in `evalkit.yaml` already make `evalkit run` exit 1 when a model misses one, so
a CI job fails on a regression. For release gates on any figure, including interval bounds and
warn-only gates, write a gates file and check the run's summary with `evalkit gate`, which
makes no model call and needs no key:

```yaml
gates:
  - metric: headline
    labels: {model: "*", judge: "*"}   # every model, every judge
    op: ">="
    value: 0.6
  - metric: citation_validity
    labels: {model: "*"}
    bound: lower                       # the 95% interval's lower bound
    op: ">="
    value: 0.9
    severity: warn                     # reported, never fails the job
```

```bash
evalkit gate runs/r-abc123/summary.json --config gates.yaml --out gate.json
```

![evalkit gate: one row per gate and matched metric, with bound, threshold, observed value and interval, severity and result, then the verdict](assets/screens/gate.png)

It prints one row per gate and matched metric, then the verdict, and writes `gate.json` on
every exit path: 0 every fail gate holds, 1 a fail gate failed, 2 bad input (a gate that
matches no metric is an error, never a silent pass), 3 a run stopped at its cost cap, 4
judging pending. Projects that do not use `evalkit run` can gate their own figures by writing
a small [`metrics-1`](gates.md#the-metrics-1-format-frozen-2026-11-17) file.

To run it all in GitHub Actions, copy [the workflow example](../examples/github-action/evalkit.yml):
it estimates, runs, gates, caches every call and run folder between jobs (so a re-run resumes
for free), uploads only the report and summary, and skips pull requests from forks, which get
no secrets.

## 10. Compare two runs

After changing a prompt, a model or the data, compare the new run with the old one:

```bash
evalkit compare runs/r-old runs/r-new --gates gates.yaml --md compare.md
```

![evalkit compare: per metric and label, old and new values, the change with its 95% interval, pairing and flag, then the verdict](assets/screens/compare.png)

Each change gets a 95% interval, paired over shared cases when the runs share enough of them.
A change is claimed only from at least 10 shared cases and a non-zero-width interval, and it
is flagged only after a Holm correction across every compared row. With `--gates`, a flagged
move toward failing a gate exits 1. The comparison also lists every setting that differs
between the runs (code version, data snapshot, config), so you know what you compared.
Held-out rows only ever appear as split-level aggregates. More: [compare](compare.md).

## 11. Track every run with the ledger

Gates judge one run against fixed bars and compare judges two runs; the run history judges a
run against your own history and tells you when a metric has moved outside what is normal for
it.

```bash
evalkit history add runs/r-abc123 --ledger evals/runs.jsonl --gates gates.yaml
evalkit history show --ledger evals/runs.jsonl --html history.html
evalkit report RUN_DIR --history evals/runs.jsonl
```

![The trend page: one metric over seven runs, with each run's median line, warn and alarm bands, settings changes as markers and an alarm on the last run](assets/screens/ledger-trend.png)

- Each (metric, labels) is judged against the last 20 complete runs: `learning` until five
  are in, then `warn` beyond 2 spreads from the median and `alarm` beyond 3 (exit 1). With
  `--gates`, only a move toward failing the gate alarms.
- A run with judging pending is refused unrecorded (exit 4); a run stopped at its cap is
  recorded but kept out of every band (exit 3). After a deliberate change, such as a new price
  table, restart a band with `history reset`.
- Every line is hash-chained, so an edited, dropped or reordered line shows. In CI, run
  `history verify` against the push base so a cut tail shows too
  ([ledger](ledger.md#every-line-is-chained)).
- One ledger holds one project: an EvalKit run's project is its task name (`my-task` here).

Record what each paid run cost and alarm before the next one would break your budget:

```bash
evalkit spend add --kind run --usd 0.21 --run-id r-abc123 --project my-task --spend-ledger evals/spend.jsonl
evalkit spend check --spend-ledger evals/spend.jsonl --cap 25 --add-estimate 0.21
```

`spend check` exits 3 when this month's spend plus the estimate would reach the cap. For tuning
experiments with a freeze guard that allows one held-out look per frozen candidate, see
[experiments](ledger.md#experiments-and-the-freeze-guard).

## 12. Check for drift

When your production inputs move away from the cases you evaluated on, the evaluation stops
describing what users see. Freeze a reference batch once, then check each new batch against
it:

```bash
evalkit drift reference --data inputs-2026-10.csv --features question_words,n_sentences,topic --categorical topic --out drift-ref.json
evalkit drift check --reference drift-ref.json --data inputs-2026-11.csv --out drift.json --emit-metrics drift-metrics.json --project my-task
evalkit history add drift-metrics.json --ledger evals/runs.jsonl
```

`drift check` reports a PSI per feature (`ok` below 0.1, `warn` to 0.25, `alarm` above) and,
for a scored model with `--prob` and `--label`, Brier, ECE and reliability-bin changes with
95% intervals. It exits 1 on an alarm. Rows never leave the process: the outputs hold bin
edges, shares and aggregates only. `--emit-metrics` writes the figures as metrics-1, so they
can be gated and added to the run history, where the trend page charts PSI per feature. More:
[drift](drift.md).

## Going further

- **Keep your held-out split honest** ([heldout](heldout.md)): pin it with a manifest of
  hashes, score each held-out case once per rubric, fail the build when held-out text leaks
  into a tracked file, and keep a tamper-evident log:

  ```bash
  python -m evalkit.heldout manifest --split heldout=heldout/cases.jsonl --split public=data/cases.jsonl --out heldout/MANIFEST.json
  python -m evalkit.heldout scan --cases heldout/cases.jsonl --staged
  ```

- **Attack your agent** ([security](security.md)): OWASP LLM Top 10 attack cases with planted
  canaries, k trials and pass^k, which defence stopped each attack, and a model-free scripted
  attacker that checks your tools hold on their own. Its `metrics.json` gates with
  `evalkit gate` like any other.

  ![The security kit's summary: pass^k, trial success rate and blocked rate per split, stopping layers, and public categories](assets/screens/security-summary.png)

- **Check for contamination** ([contamination](contamination.md)): rerun PubMedQA questions
  in reworded form and see whether accuracy drops.
- **Evaluate your own system**, not just a model, through a system adapter, and add your own
  scorers ([plugins](plugins.md#evaluating-your-own-system-020)).
- **See every project side by side** in the [cross-project eval index](index.md), live at
  <https://biplob819.github.io/evalkit/index/>.
- **Upgrading** from 0.3: [versioning](versioning.md#03--04-migration) and the
  [per-project guide](downstream.md).
