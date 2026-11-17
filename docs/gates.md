# Gates on any metric

`evalkit gate` checks release gates against a small results file, with no model call, no
network and no API key. It reads either a **`metrics-1`** file, which any project can write
without running `evalkit run`, or EvalKit's own `summary.json`.

```
evalkit gate results/metrics.json --config gates.yaml
evalkit gate runs/r-abc123/summary.json --json --out gate.json
```

With no `--config`, an EvalKit `summary.json` is gated on the run's own v0.2 thresholds (every
model must have `passes_threshold` true, the same verdict `evalkit run` gives). A `metrics-1`
file needs a gates file.

## The `metrics-1` format (frozen 2026-11-17)

```json
{
  "schema_version": "metrics-1",
  "project": "assay-pipeline",
  "run_id": "main-2026-11-16",
  "created_at": "2026-11-16T21:04:00Z",
  "simulated": false,
  "provenance": {"code_version": "<git sha>", "snapshot": null, "config_hash": null, "evalkit": null},
  "metrics": [
    {"name": "attack_successes", "unit": "count", "value": 0, "n": 150,
     "labels": {"model": "gpt-6.1-sol", "split": "public"}},
    {"name": "attack_trial_success_rate", "unit": "rate", "value": 0.0, "n": 150, "k": 0,
     "labels": {"model": "gpt-6.1-sol"}},
    {"name": "revenue_per_member", "unit": "NTD", "value": 12.3, "n": 1000,
     "ci_low": 11.1, "ci_high": 13.4, "ci_method": "block-bootstrap-1000; Holm",
     "labels": {"policy": "bandit", "variant": "central"}},
    {"name": "audit_log_verified", "unit": "bool", "value": true},
    {"name": "spend", "unit": "USD", "value": 1.23}
  ]
}
```

- **Top level**: `schema_version` (exactly `metrics-1`), `project`, `run_id` and `created_at`
  (ISO 8601 with a zone) are required; `simulated` (default false), `provenance` (four optional
  strings, or `null` for none) and `metrics` (default empty) are optional. Any other key is
  refused, so a typo cannot pass silently. Duplicate keys and `NaN`/`Infinity` are refused too,
  and so are integers longer than 4300 digits and nesting deeper than Python's recursion limit
  (each with its own message).
- **A metric**: `name` (`[a-z][a-z0-9_.]*`, the whole string: no trailing newline), `unit` and
  `value` are required. `unit` is `count`, `rate` (a share in [0, 1]), `bool`, `ratio`, `ms`,
  `s`, or an upper-case code (`USD`, `NTD`, …). The value must fit its unit: a JSON integer ≥ 0
  for `count` (`1.0` is not a count), [0, 1] for `rate`, `true`/`false` for `bool`, a finite
  number otherwise. Numbers are checked as parsed, without converting integers to floats.
- `n` (integer ≥ 0), `k` (integer, 0 ≤ k ≤ n, only on a `rate`, needs `n`), `ci_low` and
  `ci_high` (both or neither, `ci_low ≤ value ≤ ci_high`) and `ci_method` (required with an
  interval) are optional. A metric object takes no other key.
- A `rate` with `k` and `n` must have `value` = k / n, within 1e-6 relative or 5e-7 absolute
  (half a unit in the sixth decimal, so a value rounded to 6 decimals passes: 1/150 may be
  written `0.006667`). A `rate`'s interval lies in [0, 1]; a `count`'s `ci_low` is ≥ 0.
- **Intervals**: a supplied interval is carried exactly as given, never recomputed. A `rate`
  with `k` and `n` and no interval gets an exact 95% Clopper-Pearson interval (`ci_method:
  clopper-pearson`, below).
- **Labels**: a flat object of string keys to string, integer or boolean values. `(name,
  labels)` is unique in a file. Label values are compared with their type: `1`, `"1"` and
  `true` are different values (in gates and in disparity groups alike). The one exception is
  the held-out check: a `split` value is compared with `heldout_splits` as a canonical string
  (`2024` and `"2024"` alike, `true` as `"true"`), so a split written as a number is still held
  out.
- **No case or member ids**: a label key that names an id (`id`, `case`, `member`, `msno`,
  `item`, `row`, their plurals, or anything ending in `_id`/`_ids`) is refused. Held-out splits
  appear only as split-level metrics.

### Standard efficiency names (D-67)

Every project uses these names per model (label `model`), so cost, tokens and latency compare
across projects. A metric with one of these names must use its unit.

| Name | Unit |
|---|---|
| `tokens_in_per_case`, `tokens_out_per_case`, `tokens_reasoning_per_case`, `tokens_cached_per_case` | `count` (a mean per case, so it may be fractional; give `n`) |
| `cache_hit_share`, `retry_rate`, `error_rate` | `rate` (rates with `k` and `n`) |
| `cost_per_case`, `cost_per_correct`, `eval_overhead_cost`, `system_cost` | a currency code (`USD`, …) |
| `latency_p50`, `latency_p95`, `latency_p99` | `ms` (`s` accepted) |

Optional metric labels: `mode` (`batch` or `live`) and `price_date`. Latency percentiles and
per-case cost gate like any other metric.

### EvalKit's `summary.json` in this shape

`evalkit gate` converts an EvalKit summary (`evalkit.metrics.from_run_summary`): per model,
`accuracy`, `citation_validity`, `coverage`, `headline` and `groundedness` (label `judge`),
`citation_precision` and `aim_citation_rate` (per judge), `task_scores.<scorer>`,
`cost_per_100_usd`, `cost_per_correct_usd`, `latency_p50_ms`, `latency_p95_ms`, `error_rate`
and the bool `passes_threshold`, each labelled `model`; slices add their field as a label
(`rule: R1`, `split: heldout`). The efficiency block is added from each model's `efficiency` in
`summary.json` (below): token means, `cache_hit_share`, `error_rate`, `cost_per_case` and
`cost_per_correct` (USD), `latency_p50/p95/p99` (ms), the last five with seeded 95% bootstrap
intervals over cases, `system_cost` and `eval_overhead_cost` (USD), labelled `mode: live` and
`price_date`. A summary written before 0.3 has no `efficiency`; then, when the run's
`records.jsonl` sits beside it, the block is computed from the records with the same seeded
resamples (without `eval_overhead_cost` and `price_date`, which records do not carry). The run
report shows the same block.

### Efficiency in `summary.json` (0.3, EFF-02, D-70)

Since 0.3, `summary.json` gains an optional `efficiency` object on each model (left out when a
summary has no records to compute it from, so older summaries keep their bytes and readers
that do not know it ignore it). It describes the model's generation calls, the system under
test, over every case:

| Field | Meaning |
|---|---|
| `mode` | `live` (EvalKit's generation calls are not batched) |
| `price_date` | the price table's `checked_on` date for the model (`prices.yaml`) when the summary was written; null for a model without an entry (an endpoint or system) |
| `n` | cases |
| `tokens_in_per_case`, `tokens_out_per_case`, `tokens_reasoning_per_case`, `tokens_cached_per_case` | means per case |
| `cache_hit_share` | cached input tokens over input tokens (null with no input tokens) |
| `cost_per_case_usd`, `cost_per_correct_usd` | `{point, lo, hi}`: mean cost per case, and total cost over correct answers (null when none was correct; `n_correct` counts them) |
| `latency_p50_ms`, `latency_p95_ms`, `latency_p99_ms` | `{point, lo, hi}`: nearest-rank percentiles over calls that reached the provider (`n_latency`; transport failures excluded) |
| `error_rate`, `n_errored` | failed calls over cases, and their count |
| `retry_rate`, `n_retried` | always null: retries happen inside the adapter call and are not recorded per call, so they cannot be derived from a run's records |
| `system_cost_usd` | total generation cost |
| `eval_overhead_cost_usd` | the extractor's and judges' cost (claim judges and, for tasks that ask for it, citation judges) for this model's answers, from the cost stored with each cached call, so it is the same on every rerun; null when a summary was aggregated without it |

The four `{point, lo, hi}` figures carry seeded 95% percentile bootstrap intervals from the same
case resamples as every other interval in the summary. Costs are list prices of the calls
(cached calls keep the cost they had when made), not the run folder's real spend.

## The gates file

```yaml
gates:
  - metric: attack_successes
    labels: {model: "*"}        # every value of the label; omit labels to match every metric of that name
    op: "<="                    # >=, <=, ==
    value: 0
    severity: fail              # fail (default) or warn
  - metric: attack_trial_success_rate
    bound: upper                # point (default), lower, upper: the 95% CI bound
    op: "<="
    value: 0.03
  - metric: audit_log_verified
    op: "=="
    value: true
  - metric: offer_rate
    disparity: age_band         # the gap between the highest and lowest group
    op: "<="
    value: 0.05
heldout_splits: [heldout]       # values of the `split` label shown only as aggregates
```

The gates file is YAML 1.1, so an unquoted `yes`, `no`, `on`, `off`, `true` or `false` is a
boolean, not a string: `labels: {answered: no}` matches the boolean `false`, and `value: yes`
is a true/false gate. Quote such words (`labels: {answered: "no"}`) when the label is a string.
Duplicate keys in a mapping are refused with their line.

- A gate checks **every** metric it matches: the metric's name, and each label the gate lists
  (`"*"` means the label must be present, with any value). A metric may carry more labels than
  the gate lists.
- A gate that names no metric, or whose labels match none, is an error (exit 2), never a silent
  pass. `bound: lower|upper` on a metric without an interval is an error (exit 2), except a
  `rate` with `k` and `n`, which gets the exact interval.
- `true`/`false` gates use `==` on a `bool` metric; a numeric gate on a `bool` metric (or the
  reverse) is an error.
- `severity: warn` gates that fail are printed and listed in `gate.json` but do not fail the
  run.
- **Disparity** (`disparity: <label>`): the metrics that carry that label are grouped by every
  other label; for each combination the gate checks the gap between the highest and the lowest
  group value. When every group is a `rate` with `n` (and `k`, or `k = round(value × n)`), the
  gap has a 95% interval: 10,000 seeded draws, each taking one value per group from its
  Clopper-Pearson confidence distribution (an even mix of Beta(k, n−k+1) and Beta(k+1, n−k)),
  percentile bounds. `bound: upper` gates the upper bound of the gap. A combination with fewer
  than two groups is an error. Groups are told apart by type and value (`1` and `"1"` are two
  groups; the note then quotes the string one).
- **How to read the disparity interval**: the largest minus the smallest of several noisy
  group rates is biased upward, since sampling noise widens the observed spread, and more so
  with more groups and smaller n. When the true gap is 0 the interval often excludes 0, so its
  lower bound is not evidence of a gap. Its upper bound is conservative (it tends to sit above
  the true gap), which is the safe direction for a `bound: upper` gate with `<=`: a gate it
  passes holds, but a fair system with small groups can fail it.

## Exact intervals for rates

`evalkit.stats.clopper_pearson(k, n, alpha=0.05, sided="two")` is the exact (Clopper-Pearson)
binomial interval, with one convention per use (OWNER D-70):

- **Displayed intervals** (`sided="two"`, the default; `ci_low`/`ci_high` in gate output and
  intervals in reports): the standard two-sided interval, alpha/2 in each tail for every k,
  k = 0 and k = n included. Lower = B(alpha/2; k, n−k+1) (0 at k = 0), upper =
  B(1−alpha/2; k+1, n−k) (1 at k = n). n = 150: k = 0 gives 0 to 0.0243 (= 1 − 0.025^(1/150)),
  k = 1 gives 0.00017 to 0.0366.
- **Gate bounds** (`sided="upper"` / `sided="lower"`; what `bound: upper|lower` checks on a rate
  with `k` and `n` and no supplied interval): the exact one-sided 95% bound, the whole alpha in
  one tail, for every k. Upper = B(1−alpha; k+1, n−k), which is 1 − alpha^(1/n) at k = 0; lower
  = B(alpha; k, n−k+1), alpha^(1/n) at k = n. n = 150: k = 0 gives 0.0198 (passes a 0.03 gate),
  k = 1 gives 0.0312 (fails it). The gate row's note says which bound was gated
  (`gated on the one-sided 95% upper bound 0.0312`), while `ci_low`/`ci_high` show the two-sided
  interval. A supplied interval is gated on its own bounds, as given.

Reports never write a zero rate as "0%" or as a zero-width interval. "Below X% at 95%" is a
one-sided statement, so `evalkit.stats.rate_wording(0, 150)` gives `0 of 150, below 1.98% at
95%` (and `150 of 150, above 98% at 95%`); other counts read `1 of 150 (0.667%, 95% CI
0.0169%–3.66%)`. The run report uses the same wording for any share over all cases that is
exactly 0 or 1 (`0 of 12, below 22.1% at 95%`); a share without a case count (citation
precision) reads `none observed (no interval)`.

## Output and exit codes

`evalkit gate` prints whether the results are **simulated** first, then one row per gate and
matched metric (metric, labels, bound, op, threshold, observed, severity, result), notes, and
the verdict. `--json` prints the same object that is written to `gate.json` (`--out`, default
`./gate.json`); `gate.json` is written on every exit path, including exit 2, with the errors.
It is written atomically (a temporary file beside it, then a rename); an `--out` that names a
folder, or that cannot be written, exits 2 and leaves no temporary file. A command-line error
(an unknown option, a missing summary path) exits 2 from the argument parser before anything
is read; when `--out` was given, gate.json is still written there with the error.

| Exit | Meaning |
|---|---|
| 0 | every `fail` gate holds (failed `warn` gates are listed) |
| 1 | a `fail` gate fails |
| 2 | bad input: unreadable file, schema error, unknown metric, a gate matching nothing, a bound without an interval, a value outside its unit's domain |
| 3 | an EvalKit `summary.json` from a run stopped at its cost cap (`run.json` beside it says `stopped_at_cost_cap`) |
| 4 | an EvalKit `summary.json` with judging pending |

When several apply, the precedence is **2 > 3 > 4 > 1 > 0**.

**Held-out splits**: rows whose `split` label is in `heldout_splits` (compared as canonical
strings, so `split: 2024` matches `heldout_splits: ["2024"]` or `[2024]`) are printed as one
split-level row per gate, split, model and judge: the number of rows, the worst result and the
worst observed value; their other labels are never shown, error rows included. The verdict
still checks every row.
The format has no case ids, and free-text label values longer than 64 characters are never
echoed (they print as `<N characters, not shown>`); error messages never quote file values.

## Input classes

Every input the verdict reads is listed here (`evalkit.gates.INPUTS`), and a test fails if the
verdict reads anything else. **Input-only** checks always exit 2. **Completion-dependent**
checks exit 2 on a finished input, 3 when an EvalKit run stopped at its cost cap, and 4 while
its judging is pending. Only the per-judge figures are completion-dependent: they are empty
until every judge finishes, so a gate on `headline`, `groundedness`, `citation_precision` or
`aim_citation_rate` that matches nothing in an unfinished run exits 3 or 4. Any other gate
naming no metric (an unknown metric) or matching nothing is input-only and exits 2, even while
judging is pending. A `metrics-1` file is always finished.

Prefixes: `file:` a file as a whole; `metrics.` the metrics-1 object; `gates.` the gates file;
`status.` the completion state the verdict uses; `summary.` an EvalKit `summary.json`; `run.`
the `run.json` beside it; `records[].` the `records.jsonl` beside it (aggregated for the
efficiency block, never printed). `[]` is every element of a list, `{}` every value of a map.

| Input | Class | Exit when it fails |
|---|---|---|
| `file:summary` | input-only | 2 |
| `file:gates` | input-only | 2 |
| `file:run.json` | input-only | 2 |
| `file:records.jsonl` | input-only | 2 |
| `metrics.schema_version` | input-only | 2 |
| `metrics.project` | input-only | 2 |
| `metrics.run_id` | input-only | 2 |
| `metrics.created_at` | input-only | 2 |
| `metrics.simulated` | input-only | 2 |
| `metrics.provenance` | input-only | 2 |
| `metrics.metrics` | input-only | 2 |
| `metrics.metrics[].name` | input-only | 2 |
| `metrics.metrics[].labels` | input-only | 2 |
| `metrics.metrics[].unit` | input-only | 2 |
| `metrics.metrics[].value` | input-only | 2 |
| `metrics.metrics[].n` | input-only | 2 |
| `metrics.metrics[].k` | input-only | 2 |
| `metrics.metrics[].ci_low` | input-only | 2 |
| `metrics.metrics[].ci_high` | input-only | 2 |
| `metrics.metrics[].ci_method` | input-only | 2 |
| `gates.gates` | input-only | 2 |
| `gates.gates[].metric` | input-only | 2 |
| `gates.gates[].labels` | input-only | 2 |
| `gates.gates[].op` | input-only | 2 |
| `gates.gates[].value` | input-only | 2 |
| `gates.gates[].bound` | input-only | 2 |
| `gates.gates[].severity` | input-only | 2 |
| `gates.gates[].disparity` | input-only | 2 |
| `gates.heldout_splits` | input-only | 2 |
| `status.kind` | input-only | 2 |
| `status.judging` | completion-dependent | 2, 3 or 4 |
| `status.stopped_at_cost_cap` | completion-dependent | 2, 3 or 4 |
| `summary.schema_version` | input-only | 2 |
| `summary.judging_status` | completion-dependent | 2, 3 or 4 |
| `run.run_id` | input-only | 2 |
| `run.stopped_at_cost_cap` | completion-dependent | 2, 3 or 4 |
| `summary.run_id` | input-only | 2 |
| `summary.task` | input-only | 2 |
| `summary.config_hash` | input-only | 2 |
| `summary.started_at` | input-only | 2 |
| `summary.finished_at` | input-only | 2 |
| `summary.models` | input-only | 2 |
| `summary.slices` | input-only | 2 |
| `summary.models[].n` | input-only | 2 |
| `summary.models[].n_errors` | input-only | 2 |
| `summary.models[].cost_per_100_usd` | input-only | 2 |
| `summary.models[].latency_p50_ms` | input-only | 2 |
| `summary.models[].latency_p95_ms` | input-only | 2 |
| `summary.models[].model` | input-only | 2 |
| `summary.models[].task_scores` | input-only | 2 |
| `summary.models[].accuracy` | input-only | 2 |
| `summary.models[].citation_validity` | input-only | 2 |
| `summary.models[].coverage` | input-only | 2 |
| `summary.slices[].n` | input-only | 2 |
| `summary.slices[].n_errors` | input-only | 2 |
| `summary.slices[].cost_per_100_usd` | input-only | 2 |
| `summary.slices[].latency_p50_ms` | input-only | 2 |
| `summary.slices[].latency_p95_ms` | input-only | 2 |
| `summary.slices[].model` | input-only | 2 |
| `summary.slices[].task_scores` | input-only | 2 |
| `summary.slices[].accuracy` | input-only | 2 |
| `summary.slices[].citation_validity` | input-only | 2 |
| `summary.slices[].coverage` | input-only | 2 |
| `summary.models[].headline` | completion-dependent | 2, 3 or 4 |
| `summary.models[].groundedness_mean` | completion-dependent | 2, 3 or 4 |
| `summary.models[].citation_precision` | completion-dependent | 2, 3 or 4 |
| `summary.models[].aim_citation_rate` | completion-dependent | 2, 3 or 4 |
| `summary.slices[].headline` | completion-dependent | 2, 3 or 4 |
| `summary.slices[].groundedness_mean` | completion-dependent | 2, 3 or 4 |
| `summary.slices[].citation_precision` | completion-dependent | 2, 3 or 4 |
| `summary.slices[].aim_citation_rate` | completion-dependent | 2, 3 or 4 |
| `summary.models[].accuracy.point` | input-only | 2 |
| `summary.models[].accuracy.lo` | input-only | 2 |
| `summary.models[].accuracy.hi` | input-only | 2 |
| `summary.models[].citation_validity.point` | input-only | 2 |
| `summary.models[].citation_validity.lo` | input-only | 2 |
| `summary.models[].citation_validity.hi` | input-only | 2 |
| `summary.models[].coverage.point` | input-only | 2 |
| `summary.models[].coverage.lo` | input-only | 2 |
| `summary.models[].coverage.hi` | input-only | 2 |
| `summary.slices[].accuracy.point` | input-only | 2 |
| `summary.slices[].accuracy.lo` | input-only | 2 |
| `summary.slices[].accuracy.hi` | input-only | 2 |
| `summary.slices[].citation_validity.point` | input-only | 2 |
| `summary.slices[].citation_validity.lo` | input-only | 2 |
| `summary.slices[].citation_validity.hi` | input-only | 2 |
| `summary.slices[].coverage.point` | input-only | 2 |
| `summary.slices[].coverage.lo` | input-only | 2 |
| `summary.slices[].coverage.hi` | input-only | 2 |
| `summary.models[].headline{}.point` | input-only | 2 |
| `summary.models[].headline{}.lo` | input-only | 2 |
| `summary.models[].headline{}.hi` | input-only | 2 |
| `summary.models[].groundedness_mean{}.point` | input-only | 2 |
| `summary.models[].groundedness_mean{}.lo` | input-only | 2 |
| `summary.models[].groundedness_mean{}.hi` | input-only | 2 |
| `summary.models[].citation_precision{}.point` | input-only | 2 |
| `summary.models[].citation_precision{}.lo` | input-only | 2 |
| `summary.models[].citation_precision{}.hi` | input-only | 2 |
| `summary.models[].aim_citation_rate{}.point` | input-only | 2 |
| `summary.models[].aim_citation_rate{}.lo` | input-only | 2 |
| `summary.models[].aim_citation_rate{}.hi` | input-only | 2 |
| `summary.models[].task_scores{}.point` | input-only | 2 |
| `summary.models[].task_scores{}.lo` | input-only | 2 |
| `summary.models[].task_scores{}.hi` | input-only | 2 |
| `summary.slices[].headline{}.point` | input-only | 2 |
| `summary.slices[].headline{}.lo` | input-only | 2 |
| `summary.slices[].headline{}.hi` | input-only | 2 |
| `summary.slices[].groundedness_mean{}.point` | input-only | 2 |
| `summary.slices[].groundedness_mean{}.lo` | input-only | 2 |
| `summary.slices[].groundedness_mean{}.hi` | input-only | 2 |
| `summary.slices[].citation_precision{}.point` | input-only | 2 |
| `summary.slices[].citation_precision{}.lo` | input-only | 2 |
| `summary.slices[].citation_precision{}.hi` | input-only | 2 |
| `summary.slices[].aim_citation_rate{}.point` | input-only | 2 |
| `summary.slices[].aim_citation_rate{}.lo` | input-only | 2 |
| `summary.slices[].aim_citation_rate{}.hi` | input-only | 2 |
| `summary.slices[].task_scores{}.point` | input-only | 2 |
| `summary.slices[].task_scores{}.lo` | input-only | 2 |
| `summary.slices[].task_scores{}.hi` | input-only | 2 |
| `summary.models[].cost_per_correct_usd` | input-only | 2 |
| `summary.models[].efficiency` | input-only | 2 |
| `summary.models[].efficiency.mode` | input-only | 2 |
| `summary.models[].efficiency.price_date` | input-only | 2 |
| `summary.models[].efficiency.n` | input-only | 2 |
| `summary.models[].efficiency.tokens_in_per_case` | input-only | 2 |
| `summary.models[].efficiency.tokens_out_per_case` | input-only | 2 |
| `summary.models[].efficiency.tokens_reasoning_per_case` | input-only | 2 |
| `summary.models[].efficiency.tokens_cached_per_case` | input-only | 2 |
| `summary.models[].efficiency.cache_hit_share` | input-only | 2 |
| `summary.models[].efficiency.cost_per_case_usd` | input-only | 2 |
| `summary.models[].efficiency.cost_per_correct_usd` | input-only | 2 |
| `summary.models[].efficiency.n_correct` | input-only | 2 |
| `summary.models[].efficiency.latency_p50_ms` | input-only | 2 |
| `summary.models[].efficiency.latency_p95_ms` | input-only | 2 |
| `summary.models[].efficiency.latency_p99_ms` | input-only | 2 |
| `summary.models[].efficiency.n_latency` | input-only | 2 |
| `summary.models[].efficiency.error_rate` | input-only | 2 |
| `summary.models[].efficiency.n_errored` | input-only | 2 |
| `summary.models[].efficiency.retry_rate` | input-only | 2 |
| `summary.models[].efficiency.n_retried` | input-only | 2 |
| `summary.models[].efficiency.eval_overhead_cost_usd` | input-only | 2 |
| `summary.models[].efficiency.system_cost_usd` | input-only | 2 |
| `summary.models[].efficiency.cost_per_case_usd.point` | input-only | 2 |
| `summary.models[].efficiency.cost_per_case_usd.lo` | input-only | 2 |
| `summary.models[].efficiency.cost_per_case_usd.hi` | input-only | 2 |
| `summary.models[].efficiency.cost_per_correct_usd.point` | input-only | 2 |
| `summary.models[].efficiency.cost_per_correct_usd.lo` | input-only | 2 |
| `summary.models[].efficiency.cost_per_correct_usd.hi` | input-only | 2 |
| `summary.models[].efficiency.latency_p50_ms.point` | input-only | 2 |
| `summary.models[].efficiency.latency_p50_ms.lo` | input-only | 2 |
| `summary.models[].efficiency.latency_p50_ms.hi` | input-only | 2 |
| `summary.models[].efficiency.latency_p95_ms.point` | input-only | 2 |
| `summary.models[].efficiency.latency_p95_ms.lo` | input-only | 2 |
| `summary.models[].efficiency.latency_p95_ms.hi` | input-only | 2 |
| `summary.models[].efficiency.latency_p99_ms.point` | input-only | 2 |
| `summary.models[].efficiency.latency_p99_ms.lo` | input-only | 2 |
| `summary.models[].efficiency.latency_p99_ms.hi` | input-only | 2 |
| `summary.models[].passes_threshold` | input-only | 2 |
| `summary.slices[].field` | input-only | 2 |
| `summary.slices[].value` | input-only | 2 |
| `records[].run_id` | input-only | 2 |
| `records[].case_id` | input-only | 2 |
| `records[].seed` | input-only | 2 |
| `records[].scores` | input-only | 2 |
| `records[].scores.label_correct` | input-only | 2 |
| `records[].completion` | input-only | 2 |
| `records[].completion.model` | input-only | 2 |
| `records[].completion.tokens_in` | input-only | 2 |
| `records[].completion.tokens_out` | input-only | 2 |
| `records[].completion.tokens_reasoning` | input-only | 2 |
| `records[].completion.tokens_cached` | input-only | 2 |
| `records[].completion.cost_usd` | input-only | 2 |
| `records[].completion.latency_ms` | input-only | 2 |
| `records[].completion.error` | input-only | 2 |
