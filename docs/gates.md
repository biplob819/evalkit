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
  strings) and `metrics` (default empty) are optional. Any other key is refused, so a typo
  cannot pass silently. Duplicate keys and `NaN`/`Infinity` are refused too.
- **A metric**: `name` (`[a-z][a-z0-9_.]*`), `unit` and `value` are required. `unit` is
  `count`, `rate` (a share in [0, 1]), `bool`, `ratio`, `ms`, `s`, or an upper-case code
  (`USD`, `NTD`, …). The value must fit its unit: a JSON integer ≥ 0 for `count` (`1.0` is not
  a count), [0, 1] for `rate`, `true`/`false` for `bool`, a finite number otherwise. Numbers are
  checked as parsed, without converting integers to floats.
- `n` (integer ≥ 0), `k` (integer, 0 ≤ k ≤ n, only on a `rate`, needs `n`), `ci_low` and
  `ci_high` (both or neither, `ci_low ≤ value ≤ ci_high`) and `ci_method` (required with an
  interval) are optional. A metric object takes no other key.
- **Intervals**: a supplied interval is carried exactly as given, never recomputed. A `rate`
  with `k` and `n` and no interval gets an exact 95% Clopper-Pearson interval (`ci_method:
  clopper-pearson`, below).
- **Labels**: a flat object of string keys to string, integer or boolean values. `(name,
  labels)` is unique in a file. Label values are compared with their type: `1`, `"1"` and
  `true` are different values.
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
(`rule: R1`, `split: heldout`). When the run's `records.jsonl` sits beside the summary, the
efficiency block is added from it: token means, `cache_hit_share`, `error_rate`,
`cost_per_case` and `cost_per_correct` (USD) and `latency_p50/p95/p99` (ms), the last five with
seeded 95% bootstrap intervals over cases (label `mode: live`). The run report shows the same
block. `summary.json` itself is unchanged.

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
  than two groups is an error.

## Exact intervals for rates

`evalkit.stats.clopper_pearson(k, n, alpha=0.05)` is the exact binomial interval. At k = 0 the
lower bound is 0, so the whole alpha goes to the upper side: upper = 1 − alpha^(1/n) (n = 150:
0.0198). At k = n, lower = alpha^(1/n). Otherwise the tails are equal, alpha/2 each, and the
bounds are beta quantiles (n = 150, k = 1: 0.00017 to 0.0366). Reports never write a zero rate
as "0%": `evalkit.stats.rate_wording(0, 150)` gives `0 of 150, below 1.98% at 95%`.

## Output and exit codes

`evalkit gate` prints whether the results are **simulated** first, then one row per gate and
matched metric (metric, labels, bound, op, threshold, observed, severity, result), notes, and
the verdict. `--json` prints the same object that is written to `gate.json` (`--out`, default
`./gate.json`); `gate.json` is written on every exit path, including exit 2, with the errors.

| Exit | Meaning |
|---|---|
| 0 | every `fail` gate holds (failed `warn` gates are listed) |
| 1 | a `fail` gate fails |
| 2 | bad input: unreadable file, schema error, unknown metric, a gate matching nothing, a bound without an interval, a value outside its unit's domain |
| 3 | an EvalKit `summary.json` from a run stopped at its cost cap (`run.json` beside it says `stopped_at_cost_cap`) |
| 4 | an EvalKit `summary.json` with judging pending |

When several apply, the precedence is **2 > 3 > 4 > 1 > 0**.

**Held-out splits**: rows whose `split` label is in `heldout_splits` are printed as one
split-level row per gate, split, model and judge: the number of rows, the worst result and the
worst observed value; their other labels are never shown. The verdict still checks every row.
The format has no case ids, and free-text label values longer than 64 characters are never
echoed (they print as `<N characters, not shown>`); error messages never quote file values.

## Input classes

Every input the verdict reads is listed here (`evalkit.gates.INPUTS`), and a test fails if the
verdict reads anything else. **Input-only** checks always exit 2. **Completion-dependent**
checks exit 2 on a finished input, 3 when an EvalKit run stopped at its cost cap, and 4 while
its judging is pending: a metric missing from an unfinished run may still appear (for example
`headline` before the judges finish). A `metrics-1` file is always finished.

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
| `metrics.metrics[].name` | completion-dependent | 2, 3 or 4 |
| `metrics.metrics[].labels` | completion-dependent | 2, 3 or 4 |
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
| `summary.models[].headline` | input-only | 2 |
| `summary.models[].groundedness_mean` | input-only | 2 |
| `summary.models[].citation_precision` | input-only | 2 |
| `summary.models[].aim_citation_rate` | input-only | 2 |
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
| `summary.slices[].headline` | input-only | 2 |
| `summary.slices[].groundedness_mean` | input-only | 2 |
| `summary.slices[].citation_precision` | input-only | 2 |
| `summary.slices[].aim_citation_rate` | input-only | 2 |
| `summary.slices[].task_scores` | input-only | 2 |
| `summary.slices[].accuracy` | input-only | 2 |
| `summary.slices[].citation_validity` | input-only | 2 |
| `summary.slices[].coverage` | input-only | 2 |
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
