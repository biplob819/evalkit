# Controlling cost

EvalKit spends money only on the model calls you configure, and tells you how much before it
starts.

## Estimate first

```bash
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 50 --dry-run
```

The dry run prints the estimated cost from `prices.yaml` (list prices, dated; `evalkit
doctor` warns when they are older than 90 days) and makes no API calls. Cost grows with
cases × models, plus one extraction per answer and one judge call per claim per judge.

## Cap the spend

`max_cost_usd` in `evalkit.yaml`, or `--max-cost` on the command line, is a hard cap on what
a run folder may spend **in total, across resumes**. The executor reserves each call's
maximum cost before sending it, so a run never passes the cap. When the next call would, the
run stops with exit code 3; everything finished so far is cached.

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
