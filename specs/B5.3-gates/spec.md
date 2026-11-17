# B5.3 — Gates on any metric

**Wave 5. Depends on:** B5.2 (public stats API). **Asked by:** assay-pipeline (its owner rulings, D-62, D-64), respectful-offers (D-63), course-assistant (its OWNER D-68 → D-65), agentic-analytics (its OWNER D-75 → D-65). Decisions: D-61 to D-66.

The **metrics-summary format (`metrics-1`) below is frozen** so projects can build exporters before the code lands (2026-11-17). Changes after this date are additive only (new optional fields) and are announced to every registered project.

## Goal
Any project, whether or not it runs `evalkit run`, can write its results as a small JSON file and gate a release on them in CI with `evalkit gate`, with no model calls, and with held-out data never printed.

## Why
Five projects were writing their own gates over their own result formats: counts that must be zero, rates with upper confidence bounds, booleans, spend caps, warn-versus-fail levels and exit codes. `RunConfig.thresholds` only covers EvalKit's own model metrics on point values with `>=`/`<=`.

## 1. The metrics-summary format `metrics-1` (GAT-03, GAT-05)

```json
{
  "schema_version": "metrics-1",
  "project": "assay-pipeline",
  "run_id": "main-2026-11-16",
  "created_at": "2026-11-16T21:04:00Z",
  "simulated": false,
  "provenance": {"code_version": "<git sha>", "snapshot": "<id or null>",
                 "config_hash": "<sha256 or null>", "evalkit": "<version or null>"},
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

Rules:
- Top level: `schema_version` (exactly `metrics-1`), `project`, `run_id`, `created_at` (ISO 8601 with zone) required; `simulated` (default false), `provenance` (all fields optional, default null) optional. Unknown top-level keys are refused (a typo must not pass silently).
- A metric: `name` (`[a-z][a-z0-9_.]*`), `unit`, `value` required. `unit` is `count`, `rate` (a share in [0, 1]), `bool`, `ratio`, `ms`, `s`, or a currency or other unit as an upper-case code (`USD`, `NTD`, …). `value` must fit its unit: an integer ≥ 0 for `count`, [0, 1] for `rate`, true/false for `bool`, finite otherwise.
- `n` (cases or trials, integer ≥ 0), `k` (successes, integer 0 ≤ k ≤ n, only for `rate`), `ci_low`/`ci_high` (both or neither, `ci_low ≤ value ≤ ci_high`), `ci_method` (required when an interval is given) are optional.
- **Supplied intervals are carried as given, never recomputed** (respectful-offers' block bootstrap with Holm). A `rate` with `k` and `n` and no interval gets an exact 95% Clopper-Pearson interval computed by EvalKit (`ci_method: "clopper-pearson"`); with k = 0 the upper bound is `1 - 0.05**(1/n)` (GAT-07).
- `labels`: a flat object of string keys to string, integer or boolean values, as many fields as needed (`model`, `split`, `category`, `policy`, `variant`, `rule`, …). The pair (`name`, `labels`) is unique in a file.
- **No case or member ids anywhere in the file.** Held-out splits appear only as split-level metrics.
- `evalkit gate` also reads EvalKit's own `summary.json`, converting its model metrics to this shape (labels `model`, and `judge` where per judge; slices as extra labels).

## 2. Gates file (GAT-01, GAT-06, GAT-08, GAT-10)

```yaml
gates:
  - metric: attack_successes
    labels: {model: "*"}        # every label value; omit labels to match every metric of that name
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
  - metric: gold_answer_rate
    bound: lower
    op: ">="
    value: 0.6
    severity: warn
  - metric: offer_rate
    disparity: age_band         # GAT-06: gap between the highest and lowest group rate
    op: "<="
    value: 0.05
heldout_splits: [heldout]       # values of the `split` label shown only as aggregates
```
- A gate that matches no metric is an error (exit 2), never a silent pass. `bound: lower|upper` on a metric without an interval is an error (exit 2), except a `rate` with `k` and `n` (exact interval computed).
- `disparity: <label>`: for each combination of the other labels, the gap between the highest and lowest group value of that label, with a 95% interval when the group metrics carry `n` (bootstrap over groups' Clopper-Pearson draws, seeded) — the gate applies to the gap, or to its upper bound with `bound: upper`.
- Latency (`unit: ms`/`s`, names such as `latency_p50`, `latency_p95`) and per-case cost are ordinary metrics (GAT-10).

## 3. `evalkit gate` (GAT-02, GAT-04, GAT-09)
`evalkit gate <summary.json> [--config gates.yaml] [--json] [--out gate.json]`
- No model calls, no network, no keys.
- Exit codes on EvalKit's existing scheme, with precedence 2 > 3 > 4 > 1 > 0: 0 every `fail` gate holds (warns allowed); 1 a `fail` gate fails; 2 bad input (unreadable file, schema error, unknown metric, gate matching nothing, value outside its unit's domain); 3 and 4 when gating an EvalKit `summary.json` from a run stopped at its cost cap (3) or with judging pending (4).
- A written **input-classes table** in the docs lists every input the verdict reads, split into input-only checks (always exit 2) and completion-dependent checks (2, 3 or 4); a test fails if the verdict reads an input not in the table. Numbers are checked against their domain as parsed JSON numbers, without converting integers to floats first.
- Output: a table of every gate (metric, labels, bound, op, threshold, observed, severity, pass/warn/fail), then the verdict. `--json` prints the same as JSON. `gate.json` is written on every exit path, including exit 2 (with the error).
- **Held-out (GAT-04):** rows whose `split` label is in `heldout_splits` are reported as split-level aggregates only; no case id can appear because the format has none, and gate output never echoes free-text label values longer than 64 characters.
- The `simulated` flag is printed at the top of the output and in `gate.json` (GAT-05).
- The GitHub Action example gains a gate step.

## Tests from the asking projects (D-61 rule 3)
- assay-pipeline: one attack success of 150 for gpt-6.1-sol fails both the `<= 0` count gate and the upper-bound `<= 0.03` gate with exit 1; n=150, k=0 gives an upper bound of about 0.0198 (passes 0.03) and k=1 about 0.0365 (fails); warn-only failures exit 0 and are listed.
- respectful-offers: its `results/kkbox/summary.json` at 777bb1e (aggregates only), exported to `metrics-1`: violations R1/R2 `== 0` per policy; NTD unit; supplied intervals kept exactly; `simulated: true` shown; the age-band disparity gate.
- course-assistant: exit-code precedence, the input-classes test, `gate.json` on every path, domain checks without float conversion.

## Out of scope
- Comparisons between runs (B5.6) and history or trends (B5.7).
- Computing metrics from raw per-case data (a project exports aggregates).
