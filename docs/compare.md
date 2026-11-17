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

### What counts as an EvalKit run folder (0.4.0)

`run.json` and `summary.json` are common names: other projects write their own (agentic-analytics'
results folders hold both, with `schema_version: "1.0"` as EvalKit's do). A folder is an EvalKit
run only when its `summary.json` carries EvalKit's marker: a JSON object whose `schema_version`
is EvalKit's results format (`<major>.<minor>`, 1.x today) and which has every key EvalKit
always writes there (`run_id`, `task`, `config_hash`, `judge_mode`, `calibration_status`,
`judging_status`, `models`). For `run.json` the keys are `run_id`, `task`, `task_ref`,
`config_hash`, `judge_mode`, `judges` and `live_judges`. Otherwise:

- a folder whose `summary.json` is not EvalKit's is read through its `metrics.json` when it has
  one (the project's own summary is ignored); without one, compare exits 2 and says why
  (`... is not an EvalKit run folder: its summary.json has schema_version '1.0' but not
  EvalKit's summary.json keys (...)`). A `summary.json` named directly is refused the same way;
- a project's own `run.json` beside `metrics.json` does not make the folder an EvalKit run, so
  its `cases.jsonl` sidecar is read as per-case values;
- `evalkit gate` refuses a foreign `summary.json` with the same reason (exit 2), and `evalkit
  report` and a resumed `evalkit run` refuse a folder whose `run.json` is not EvalKit's.

A file that carries the marker but fails EvalKit's schema is reported as a broken EvalKit file,
never read as something else. Security run folders (`python -m evalkit.security`) have their own
`summary.json` and are compared through their `metrics.json`.

Options: `--gates FILE` (gated metrics, see [Exit codes](#exit-codes); its `heldout_splits` also
apply here), `--manifest FILE` (a [B5.14 held-out manifest](heldout.md)), `--heldout-split NAME`
(repeatable; `heldout` is always held out), `--seed` (default 7), `--resamples` (default
10000), `--json` (print compare.json), `--out` (default `compare.json`; written on every exit,
including exit 2) and `--md FILE` (the markdown report). If the markdown cannot be written,
the command exits 2 and compare.json is rewritten to record exit 2 and the error.

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
- **Counts take no sidecar.** A `count` metric (other than the per-case token means such as
  `tokens_in_per_case`) is refused with exit 2 if the sidecar has rows for it. Its change is
  always on the count scale, new count − old count, from the metrics file.
- **The sidecar must agree with the metrics file.** For each (metric, labels) the sidecar
  covers, the per-case values must average to the metric's `value` within the metrics-1 rate
  tolerance (1e-6 relative or 5e-7 absolute, so a value rounded to 6 decimals passes). The
  average is the mean of the case means (trials averaged within a case, and each row without
  a `case_id` counted as one case) or the mean over every line. Otherwise the input is
  refused with exit 2. A sidecar that contradicts its summary is never compared.

## How a change is estimated

Every (metric, labels) present in both runs gets the change **new − old**:

1. **paired**. Both runs have per-case values with case ids for this row, and the row is
   public. At least 2 case ids are shared, and the shared cases are **at least half of each
   run's cases** for the row. A bootstrap over the shared cases, then over each run's trials
   within a drawn case (two-level, as D-41 and D-51). With no trial level it is a case-only
   resample, the same as `evalkit.stats.paired`. Rows outside the shared set are counted in
   `not_paired`.
2. **unpaired** (every other row). Each run gets its own 95% interval:
   - a per-case bootstrap when it has per-case values from at least 10 cases. If that
     bootstrap has zero width (every case the same) and the values are 0/1, exact
     Clopper-Pearson over the cases is used instead.
   - otherwise the supplied `ci_low`/`ci_high`
   - otherwise exact two-sided Clopper-Pearson from `k` and `n`, or over the 0/1 per-case
     values
   - otherwise a bootstrap of the few cases there are, marked `too_few_cases`

   The two intervals are combined by MOVER (Zou & Donner 2008), and `interval_sources` says
   where each came from (`bootstrap`, `supplied`, `clopper-pearson`, or
   `clopper-pearson (cases)`). A row with no interval on either side shows the change without
   an interval and is never flagged. True/false metrics show whether they changed. Every
   output labels these rows `unpaired`.

**Partial overlap.** When both runs carry case ids for a row but the shared cases are fewer
than half of either run's cases (or fewer than 2), the shared subset says little about either
run. The row is compared unpaired over all its cases and marked `partial_overlap: true`.
`n_shared` and `not_paired` state how many cases each run has without a partner, in the JSON
and in the markdown's pairing column ("partial overlap: 2 shared; 98 old and 98 new without a
partner").

### When a change is claimed

Each row's `interval_status` says whether its interval may carry a claim:

| `interval_status` | Meaning |
| --- | --- |
| `ok` | enough cases and a non-zero-width interval: takes part in Holm, can be flagged and can exit 1 |
| `too_few_cases` | a paired row with fewer than **10 shared cases**, or an unpaired side bootstrapped from fewer than 10 cases with no exact interval to use instead |
| `degenerate` | an unpaired side whose bootstrap had zero width with no exact interval to use instead. A *paired* row with at least 10 shared cases whose cases all moved by the same amount is `ok`, not degenerate: that is the strongest evidence of a change (for example, every case going from right to wrong) and it is flagged |
| `null` | no interval (a true/false metric, or no interval on a side) |

Rows that are `too_few_cases` or `degenerate` show their change, interval and p-value, but
they never enter Holm, are never flagged and never exit 1. The threshold of 10 is about
**claiming** a change. Below about ten cases, a percentile bootstrap's 95% interval badly
undercovers because it can take only a handful of distinct values (2 cases give 3 possible
resample means, 5 give 126). Two shared cases that both drop would otherwise read as a
certain regression. This is separate from the RVR-03 baseline rule (at least 2 shared case
ids for a gated metric, exit 2 below it), which is about what a baseline must contain. A
gated row can meet RVR-03 with 2 to 9 shared cases and still make no claim. For an absolute
bound on the new run alone, whatever the sample size, use `evalkit gate`.

A row is **flagged** when Holm's step-down (alpha 0.05, across every row whose
`interval_status` is `ok`) rejects it **and** its 95% interval excludes 0. Paired rows use a
bootstrap p-value (+1 correction). Unpaired rows use the normal approximation from the
half-width on the side of zero. The JSON gives both `p_value` and the Holm-adjusted `p_holm`
(null for rows outside Holm). Draws are seeded (default 7, 10,000 resamples), so the same
inputs and seed give byte-identical compare.json and markdown.

From an EvalKit run folder, these are paired on records: accuracy, citation validity,
coverage, task scores, headline and groundedness per judge (once judging is complete),
everything per slice, and the efficiency block's cost per case and error rate. Ratio figures
(citation precision, aim-citation rate, cost per correct answer), latency percentiles and token
means are compared from their intervals.

## Held-out rows

A row is held out when its `split` label is `heldout`, a `--heldout-split` value, or in the
gates file's `heldout_splits`. Held-out rows are never paired. They are printed only at split
level: one row per metric, split, model and judge, with no other label. A row that has no
label beyond those keeps its figures, since it is already a split-level aggregate. Any other
group shows only how many rows it holds and how many were flagged. That includes a group of
one row with another label, such as a category. Its figures are one slice of the split, so
they are never shown under the split's name. Held-out rows in one run only are listed under
"Missing" at split level, with how many rows they are (`heldout_rows`).

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
| `prompt_hash`, `judge_prompt_hash`, `extractor_version` | – | from the records |
| `model_ids.<model>` (one per model: its provider snapshot ids) | – | from the records |
| `judges`, `judge_mode`, `seed`, `results_format` | – | `run.json`, `summary.json` |
| anything else (`rules_hash`, `bar`, `assessments_hash`, …) | `settings.json` | `settings.json` |

`settings.json` is a flat JSON object of values or lists of values. It fills settings the
results do not record. If it disagrees with a recorded value, the input is refused. A setting
recorded in only one run is listed with the note "recorded on one side only". Run folders do
not record the EvalKit version, so when either input is a run folder and neither records it,
`evalkit` is listed as "unrecorded in both runs" rather than left out. The markdown shows a
missing value as `unrecorded`. The JSON's `settings.status` is `different`, `partly
unrecorded` (some setting is unrecorded in one run or both) or `same`.

Model snapshot ids are listed per model (`model_ids.claude-sonnet-5-5`, ...), so each fits
the 64-character display limit. Only the models whose snapshot changed are listed.

Label values are typed, as in metrics-1. When one label key carries values of different
types across the two runs, text values are quoted, as `evalkit gate` quotes disparity groups.
So `cohort=2024` (a number) and `cohort="2024"` (text) read differently in the JSON and the
markdown.

## Missing metrics are listed, never skipped

A metric (or one label combination of it) that only one run has goes in `missing`, with the
side it is missing from and whether the whole metric name is absent (`what: metric`) or only
those labels (`what: labels`). The markdown lists them under "Missing".

## Exit codes

Precedence is 2 > 1 > 0.

| Code | Meaning |
| --- | --- |
| 0 | no flagged regression in a fail-gated metric. Without `--gates` the exit code is always 0 or 2. |
| 1 | a **regression**: a row matched by a `severity: fail` gate is flagged (`interval_status` ok, Holm-adjusted, its 95% interval excludes 0) and its change moves toward failing that gate (`>=`: the change is negative; `<=`: positive; `==`: either way). Rows with too few cases, degenerate intervals or no interval never exit 1. |
| 2 | bad input: an unreadable or invalid file, sidecar or manifest; a sidecar for a count, or one that does not average to its metric's value; a unit that differs between the runs; an unwritable `--out` or `--md`; or, with `--gates`, a baseline that breaks the rules below. |

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
  - `interval_status` (`ok`, `too_few_cases`, `degenerate` or null) and `partial_overlap`
  - `p_value`, `p_holm`, `flagged`
  - `gate`, `regression`, `regression_severity`, `note`
  - `heldout_rows` (folded held-out rows only)
- `missing`, `flagged`, `regressions` and `errors`

The markdown report gives the sources, the settings differences, a table of the rows that
moved (the rest are counted, and all of them are in compare.json), the missing rows, any errors
and the verdict.
