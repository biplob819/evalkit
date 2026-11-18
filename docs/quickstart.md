# Quickstart: a first result in under five minutes

EvalKit compares Claude and OpenAI models on **your** task: accuracy, whether every claim is
backed by the sentences the answer cites, citation validity, cost and latency, with confidence
intervals and two judges from different model families.

## 1. Install (Python 3.11 or newer)

```bash
pip install 'evalkit-harness[anthropic,openai]'
evalkit --version
```

The package is `evalkit-harness`; the command and the import are both `evalkit`.

## 2. See a finished report, with no keys and no network

```bash
evalkit report --example
```

This writes `evalkit-example/report.html` from a bundled run, so you can see what a report
contains before spending anything.

## 3. Scaffold a project

```bash
evalkit init
```

`init` writes `evalkit.yaml` (models, judges, cost cap, thresholds), `.env.example`, and a
sample task in `tasks/` (`my_task.yaml` plus `my_task.jsonl` with three cases). It never
overwrites files that exist.

## 4. Add your keys and check your setup

Copy `.env.example` to `.env` and fill in `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`. `.env` is
git-ignored and EvalKit never prints key values. One provider is enough to start: in
`evalkit.yaml`, keep only that provider's models, one judge from it, and an `extractor` from
it too (for OpenAI only, for example `extractor: gpt-6-luna`).

```bash
evalkit doctor --offline
```

`doctor` checks Python, the provider SDKs, which keys are set (names only, never values) and
how old the price table is, and names anything that would stop a run. Drop `--offline` to
also check that the provider APIs are reachable.

## 5. Estimate, then run

```bash
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3 --dry-run
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3
```

The dry run prints the cost estimate and makes no API calls. The real run answers each case
with every model, splits answers into claims, has the judges check each claim against the
cited sentences, and writes `runs/<run id>/report.html` and `summary.json`. Judging uses
the providers' batch APIs at half price; if a batch is still running when `--wait` minutes
pass, the command exits with code 4: rerun the same command to collect it at no extra cost.

## 6. Try a built-in benchmark

```bash
evalkit run evalkit.yaml --task pubmedqa --n 20 --dry-run
evalkit run evalkit.yaml --task scifact --n 20 --dry-run
```

`pubmedqa` (yes / no / maybe research questions) and `scifact` (supports / refutes / not
enough info for scientific claims) download their public data on first use and verify it
against a pinned checksum. See [DATA_CARD.md](../DATA_CARD.md).

## Next

- [The user guide](guide.md): your own task end to end, from cases to a calibrated, gated
  and tracked evaluation.
- [Write your own task](writing-a-task.md) from JSONL or a spreadsheet CSV.
- [Control cost](cost.md): estimates, caps, batch judging and resume.
- [Calibrate the judges for your task](calibrate-your-task.md): until you do, every report
  says "judge not calibrated for this task".
- [Add a scorer or a model adapter](plugins.md).
- [Privacy](../PRIVACY.md): no telemetry; data goes only to the providers you configure.
