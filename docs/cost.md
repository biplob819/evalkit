# Controlling cost

EvalKit spends money only on the model calls you configure, and tells you how much before it
starts.

## Estimate first

```bash
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3 --dry-run
```

(The sample task from `evalkit init` has three cases; with your own data, set `--n` to the
number of cases you want.)

The dry run prints the estimated cost from `prices.yaml` (list prices, dated; `evalkit
doctor` warns when they are older than 90 days) and makes no API calls. Cost grows with
cases × models, plus one extraction per answer and one judge call per claim per judge.

## Claims per answer

Judging is usually the largest line, and it is planned per claim, so every estimate states
how many claims per item it assumes and where that number comes from. The dry run prints it
(and `--json` carries it as `claims`), for example:

```
claims per item: planning default 4.5, PubMedQA answers (D-46, measured 4.46); an expected count, not an upper bound
```

There are three sources:

- **Planning default (4.5).** Measured on PubMedQA benchmark answers. `evalkit run` uses it,
  because the answers do not exist until the run writes them.
- **From the texts.** When the texts to be judged already exist (explanations given to
  `faithfulness()`, or saved answers you re-judge), EvalKit counts claims from them with no
  model call: each text is planned at its word count ÷ 10, or at 4.5 if that is larger, and
  the items are summed. `estimate_faithfulness_cost(items, config=..., prices=...)` does
  this, and also sizes each judge prompt from the item's own evidence;
  `claims_from_texts(texts)` gives the count for `estimate_run_cost(..., claims=...)`.
  10 words per claim is deliberately low: EvalKit's own extractions average 11.9 words per
  claim (2,663 claims in the 553 failed answers with claims of the published 200-case run,
  `docs/results/report.html`; 12.2 in the demo replay), and a quarter of answers are below
  10.2. On those PubMedQA answers it plans 22% more claims than were extracted. Reproduce it
  with `scripts/measure_words_per_claim.py`
  (record: `docs/measurements/words-per-claim-2026-11-17.json`).
- **Measured override.** A figure you measured on your own texts, used as given: the
  `--claims-per-answer` option of the run command, `claims_per_item=12,
  claims_source="20-item sample"` in `estimate_faithfulness_cost`, or
  `measured_claims(12, "20-item sample")`.

```bash
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3 --claims-per-answer 12 --dry-run
```

To measure it, run a small paid sample of the same kind of text first, under a cap. For
faithfulness, pass about 20 items to `faithfulness()` and take the mean of
`FaithfulnessResult.n_claims`. For a benchmark task, run a few cases with a low cap, then
divide the claims in the run folder's `records.jsonl` by its answers:

```bash
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3 --max-cost 1
```

(Use about 20 answers when your dataset has them.) Twenty is enough when the texts are of
similar length; sample across lengths when they are not.

No claim count is an upper bound: a dense text can hold more claims than its length
suggests, and the tokens per call are averages. What bounds spend is the cap below.

## Cap the spend

`max_cost_usd` in `evalkit.yaml`, or `--max-cost` on the command line, caps what a run
folder may spend **in total, across resumes**. Before each live call the executor reserves
that call's maximum possible cost, so live calls never pass the cap; when the next one would,
the run stops with exit code 3 and everything finished so far is cached.

Batch judging is the exception: a batch covers thousands of claims, so it holds 1.5 times
the *expected* judge output rather than the maximum. An unusually verbose batch can therefore
end slightly above the cap. Use `--live-judges` if the cap must be strict.

## Resume for free

Every model call is cached by its exact inputs under `.evalkit/store`. Rerunning the same
command lands in the same run folder and replays finished calls at no cost, so a run stopped
by the cap, a crash or Ctrl-C picks up where it left off. Changing the config, the task or
the prompt changes the cache keys, so only the affected calls run again.

## Batch judging (the default) versus live judging

Judges run on the providers' batch APIs at half price. A batch usually finishes in minutes
but may take longer; `--wait` sets how many minutes to wait (default 30). If judging is
still pending, the run exits with code 4 and the report says "judging pending"; rerun the
same command to collect the results at no extra cost.

`--live-judges` judges immediately at full price, for when you need the answer now.

## Spend less

- Start with a small `--n` and grow it once the numbers look sensible.
- Use one judge while you iterate; keep two judges from different families for results you
  publish, since a single judge may favour its own family.
- Lower `default_effort` or `judge_effort` in `evalkit.yaml` (reasoning tokens are billed as
  output).
- Drop models you have already ruled out.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Run finished and every threshold passed |
| 1 | Run finished and a threshold failed |
| 2 | Configuration or runtime error (nothing is spent on a configuration error) |
| 3 | Stopped at the cost cap; completed work is cached, resume with the same command |
| 4 | Judging pending; rerun the same command to collect it at no extra cost |
| 130 | Interrupted (Ctrl-C); completed work is cached, rerun the same command to resume |
