# Compare two runs

`evalkit compare` tells you, per metric and label, whether a new run is better or worse than
an old one. Each change comes with a 95% interval, changes are flagged only after a
multiple-comparison correction, and held-out cases never leak. It makes no model call and no
network call, and needs no API key.

```
evalkit compare runs/r-old runs/r-new
evalkit compare results/2026-11-16/metrics.json results/2026-11-17/metrics.json --gates gates.yaml --md compare.md
```

Each of OLD and NEW can be any of these:

- an EvalKit run folder, or its `summary.json`. The per-case values come from `records.jsonl`.
  The folder's own `cases.jsonl` holds case definitions and is never read as values.
- a [`metrics-1`](gates.md#the-metrics-1-format-frozen-2026-11-17) file, or a folder that holds
  `metrics.json`. A `cases.jsonl` sidecar beside it (optional) supplies per-case values.

Either kind may also have a flat `settings.json` beside it (see [Settings](#settings)).

Options: `--gates FILE` (gated metrics, see [Exit codes](#exit-codes); its `heldout_splits` also
apply here), `--manifest FILE` (a [B5.14 held-out manifest](heldout.md)), `--heldout-split NAME`
(repeatable; `heldout` is always held out), `--seed` (default 7), `--resamples` (default
10000), `--json` (print compare.json), `--out` (default `compare.json`; written on every exit,
including exit 2) and `--md FILE` (the markdown report).

From Python:

```python
from evalkit.compare import compare_runs, render_markdown

c = compare_runs("results/old", "results/new", gates="gates.yaml")
print(c.exit_code, render_markdown(c))
```

## The per-case sidecar `cases.jsonl`

One JSON object per line:

```json
{"case_id": "atk-17", "metric": "attack_pass_k", "labels": {"model": "gpt-6.1-sol", "split": "public"}, "value": 0}
{"case_id": "q-03", "trial": 2, "metric": "rubric_pass_rate", "value": 1}
{"metric": "attack_pass_k", "labels": {"model": "gpt-6.1-sol", "split": "heldout"}, "value": 1}
```

- `metric` and `labels` must name a metric in the metrics file exactly, with the same typed
  labels. `value` is a number or true/false.
- `case_id` is optional. **Held-out rows leave it out.** A row without it is never paired.
- `trial` (an integer, 0 or more) is optional. Leave it out when the value already covers every
  trial: pass^k, for example, is computed per case from all k trials, so it is resampled over
  cases only (D-51).
- These are refused with exit 2: unknown keys, a repeated (case, trial, metric, labels), one
  case with rows both with and without `trial`, label keys that name an id, rows for a
  true/false metric, and rows whose metric or labels are not in the metrics file. Error
  messages give the line number and never echo a value or a case id.

## How a change is estimated

Every (metric, labels) present in both runs gets the change **new − old**:

1. **paired**. Both runs have per-case values with case ids for this row, the row is public,
   and at least 2 case ids are shared. A bootstrap over the shared cases, then over each run's
   trials within a drawn case (two-level, as D-41 and D-51). With no trial level it is a
   case-only resample, the same as `evalkit.stats.paired`. Rows outside the shared set are
   counted in `not_paired`.
2. **unpaired** (every other row). Each run gets its own 95% interval: a per-case bootstrap
   when it has per-case values; otherwise the supplied `ci_low`/`ci_high`; otherwise exact
   two-sided Clopper-Pearson from `k` and `n`. The two intervals are combined by MOVER (Zou &
   Donner 2008). `interval_sources` says where each came from. A row with no interval on
   either side shows the change without an interval and is never flagged. True/false metrics
   show whether they changed. Every output labels these rows `unpaired`.

A row is **flagged** when Holm's step-down (alpha 0.05, across every compared row with a
p-value) rejects it **and** its 95% interval excludes 0. Paired rows use a bootstrap p-value
(+1 correction). Unpaired rows use the normal approximation from the half-width on the side of
zero. The JSON gives both `p_value` and the Holm-adjusted `p_holm`. Draws are seeded (default
7, 10,000 resamples), so the same inputs and seed give byte-identical compare.json and
markdown.

From an EvalKit run folder, these are paired on records: accuracy, citation validity,
coverage, task scores, headline and groundedness per judge (once judging is complete),
everything per slice, and the efficiency block's cost per case and error rate. Ratio figures
(citation precision, aim-citation rate, cost per correct answer), latency percentiles and token
means are compared from their intervals.

## Held-out rows

A row is held out when its `split` label is `heldout`, a `--heldout-split` value, or in the
gates file's `heldout_splits`. Held-out rows are never paired. They are printed only at split
level: one row per metric, split, model and judge, with no other label. A group of one keeps
its figures, since it is already a split-level aggregate. A larger group shows how many rows
it holds and how many were flagged.

With `--manifest`, any per-case value whose case id is in a held-out split of the manifest
loses its id on loading, so it is never paired. No output ever prints a case id, public or
held out.

## Settings

The top of the report lists every recorded setting that differs between the two runs, so a
comparison across different settings is labelled and never silently mixed. These settings are
compared:

| Setting | metrics-1 | run folder |
| --- | --- | --- |
| `project`, `simulated` | top level | `task`; false |
| `snapshot`, `code_version`, `config_hash`, `evalkit` | `provenance` | config hash from `run.json`; code version = the records' `git_sha` |
| `models` | the distinct `model` labels | `run.json` models |
| `prompt_hash`, `model_ids`, `judge_prompt_hash`, `extractor_version` | – | from the records |
| `judges`, `judge_mode`, `seed`, `results_format` | – | `run.json`, `summary.json` |
| anything else (`rules_hash`, `bar`, `assessments_hash`, …) | `settings.json` | `settings.json` |

`settings.json` is a flat JSON object of values or lists of values. It fills settings the
results do not record. If it disagrees with a recorded value, the input is refused. A setting
recorded in only one run is listed with the note "recorded on one side only". The JSON's
`settings.status` is `different`, `partly unrecorded` or `same`.

## Missing metrics are listed, never skipped

A metric (or one label combination of it) that only one run has goes in `missing`, with the
side it is missing from and whether the whole metric name is absent (`what: metric`) or only
those labels (`what: labels`). The markdown lists them under "Missing".

## Exit codes

Precedence is 2 > 1 > 0.

| Code | Meaning |
| --- | --- |
| 0 | no flagged regression in a fail-gated metric. Without `--gates` the exit code is always 0 or 2. |
| 1 | a **regression**: a row matched by a `severity: fail` gate is flagged (Holm-adjusted, its 95% interval excludes 0) and its change moves toward failing that gate (`>=`: the change is negative; `<=`: positive; `==`: either way). |
| 2 | bad input: an unreadable or invalid file, sidecar or manifest; a unit that differs between the runs; or, with `--gates`, a baseline that breaks the rules below. |

Baseline rules (RVR-03, with `--gates`). Every gate must match a row in the old run (the
baseline) and in the new run, and each matched row must exist in both. A matched public row
with per-case values on both sides needs **at least 2 shared case ids**. A matched row compared
from summaries alone, or held out, needs n ≥ 2 on each side when n is known.

A gate matches a row exactly as in `evalkit gate`: same metric, labels with `*` for any value,
and for a disparity gate, its label present. Disparity gates and true/false gates take part in
the baseline rules, but they never decide a regression, because a gap or a flag is not one
row's change. `evalkit gate` checks those on the new run itself. Warn-gate regressions are
marked (`regression_severity: warn`) and exit 0.

Note that one more success in 150 trials is not a significant change (the interval spans 0),
so `compare` does not flag it. `evalkit gate`, which checks a bound on the new run alone, does
fail it. Use both: `gate` decides a release, `compare` says what moved.

## Output

`compare.json` (`schema_version: compare-1`) holds the following:

- `exit_code`, `verdict`, `seed`, `resamples` and `holm` (`alpha`, and `family`, the number of
  rows with a p-value)
- `old` and `new`: path, kind, project, run id, simulated, status, and whether the run has
  per-case values
- `settings`: `status` and `differences`
- `changes`, one per row, with:
  - `metric`, `labels`, `unit`, `old`, `new`, `diff`, `ci_low`, `ci_high`, `direction`
  - `pairing` and `method`
  - `interval_sources`, `n_shared`, `not_paired`, `n_old`, `n_new`
  - `p_value`, `p_holm`, `flagged`
  - `gate`, `regression`, `regression_severity`, `note`
  - `heldout_rows` (folded held-out rows only)
- `missing`, `flagged`, `regressions` and `errors`

The markdown report gives the sources, the settings differences, a table of the rows that
moved (the rest are counted, and all of them are in compare.json), the missing rows, any errors
and the verdict.
