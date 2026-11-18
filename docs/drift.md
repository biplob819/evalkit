# Drift and calibration checks

`evalkit drift` tells you whether a new batch of data, or a model's calibration, has moved away
from a frozen reference. It reports a PSI per feature and Brier, ECE and reliability-bin
changes, each with a status. Rows never leave the process: the outputs hold bin edges,
category names, shares and aggregates only. No model call, no network, no key. The run history,
its alarm bands and the trend page are in [ledger.md](ledger.md).

```
evalkit drift reference --data alerts-2026-10.jsonl --features amount,risk_score,ts_hour --out drift-ref.json
evalkit drift check --reference drift-ref.json --data alerts-2026-11.jsonl --out drift.json
evalkit drift reference --data scores.csv --features tenure,plan --categorical plan --prob p_churn --label churned --out churn-ref.json
evalkit drift reference --metrics results/2026-10/metrics.json --calibration-prefix churn --out churn-ref.json
evalkit drift check --reference churn-ref.json --metrics results/2026-11/metrics.json --config drift.yaml --emit-metrics drift-metrics.json --project respectful-offers
```

## The frozen reference

`drift reference` profiles a reference batch into `drift-ref-1`: `created_at`,
`source_sha256` (the input file's bytes), one profile per feature, the calibration figures
when asked, and `profile_sha256` over all of it. A reference that does not match its own hash
is refused (exit 2), so it cannot be edited after freezing.

Inputs are either rows or aggregates:

- **Rows:** a CSV (with a header) or a JSON Lines file, `--features a,b` (numeric unless listed
  in `--categorical`; a feature named only in `--categorical` is profiled too, by category, and
  a name that is not a column exits 2), and for calibration `--prob COLUMN --label COLUMN` (a probability in
  [0, 1] and a 0/1 outcome). Empty values, `NA`, `null` and non-finite numbers are missing:
  left out of the PSI and reported as `missing_share` per side.
- **Aggregates** (for projects that hand EvalKit no rows): a [metrics-1](gates.md) file with
  `<prefix>.feature_share` rows (unit `rate`, labels `{feature, bin}`, `n`) and the calibration
  rows `<prefix>.brier` (with its interval), `<prefix>.ece` and
  `<prefix>.reliability.predicted` / `.observed` (rates, label `bin`, `n`).

## PSI

The rule is fraud-review-bedrock's (its MON-03), so its numbers reproduce bit for bit:

- **Numeric:** 10 bins on the reference's deciles (numpy's default linear interpolation,
  repeated edges merged), bins `[e_i, e_{i+1})` with open outer bins, so a new value beyond the
  reference range lands in an end bin. A constant reference gets 3 bins: below, equal, above.
- **Categorical:** one bin per reference category plus `other` for categories it never saw.
- **Pre-binned:** the project's own bins. An export may leave an empty bin out instead of
  writing it with n = 0 (respectful-offers does), so a bin named on one side only is read as a
  share of 0 on the other, floored like any empty bin, and listed in the row's `bins_one_side`
  (`reference_only`, `new_only`). Its absence is never an error and never drift by itself; the
  PSI counts only the share the other side gives it. Two sides that share no bin at all exit 2.
- Each share is floored at 1e-4 (not renormalised), then PSI = Σ (q − p) · ln(q / p). The floor
  leaves PSI unchanged when no bin is empty and makes a bin that appears from nothing count: a
  5% share against an empty reference bin adds about 0.31, an alarm by itself.
- **Levels:** `ok` below 0.1, `warn` from 0.1 to 0.25, `alarm` above 0.25, compared unrounded
  and shown to 4 decimals. Override them per feature in the drift config.
- **Small batches:** each row states the PSI expected with no drift, (B − 1)(1/N_ref + 1/N_new)
  for B bins, and is marked `small_batch` when that is at least 0.05; the levels still apply.

## Calibration

From rows: Brier = mean (p − y)²; ECE = Σ_b (n_b / N) |ȳ_b − p̄_b| over 10 equal-width bins
(bin `floor(p · 10 + 1e-12)`, p = 1 in the last bin), with seeded bootstrap intervals over
rows (seed 7, 10,000 resamples; `--resamples` lowers it for large batches): studentized
(bootstrap-t) for Brier, so a small batch's skewed squared errors keep their coverage, and
percentile for ECE; each bin's observed rate has a Clopper-Pearson interval. Equal-width bins
align across periods.

The changes new − reference each carry a 95% interval:

| Change | Interval |
|---|---|
| Brier | MOVER from the two intervals (different periods: unpaired) |
| ECE | debiased against no drift: the delta is new − reference less the difference expected with nothing changed at the two batch sizes (`no_drift_expected`; the raw difference is `delta_raw`), with the basic interval and a two-sided p-value from that no-drift distribution |
| each bin's calibration gap (observed − mean predicted; `delta` = `gap_new` − `gap_ref`, the observed-rate change is `obs_delta`) | MOVER of the two observed rates' Clopper-Pearson intervals, shifted by the change in mean prediction (the mean predictions are held fixed) |

**Why ECE is debiased.** ECE is biased upward in a small batch: a perfectly calibrated bin
still shows |observed − predicted| > 0 from sampling alone, more so with fewer rows. Comparing
a 50-row batch with a 2,000-row reference raw would read that sampling bias as drift (a
perfectly calibrated model alarmed on every check). So the ECE change is judged against its
distribution under no drift, drawn by a seeded parametric bootstrap: with aligned bins, each
bin keeps both sides' counts and each side's own mean prediction, and each side's outcomes are
drawn at its own mean prediction plus the bin's pooled calibration gap (the null is "the gap
is unchanged", not "the observed rate is unchanged"); with bins that do not align, a batch of
the new size is drawn from the reference (bin by share, outcome by observed rate) and the
reference is redrawn at its own size.

**Why bins test the gap.** Predictions can move within their tenth with calibration unchanged
(a model that now says 0.29 where it said 0.21, and is right 29% of the time). The bin's
observed rate moves with them, so testing the observed rate read that as drift (98.7% of
such checks flagged, reference and new batch 5,000 rows each). The bin delta is the change in
the gap, observed − mean predicted, which does not move.

Measured with no drift (a seeded grid of 300 checks per cell: reference 200, 1,000 or 5,000
rows; new batch 10, 50, 200, 1,000 or 5,000 rows; aligned and unaligned bins; uniform and
skewed predictions, a stable miscalibration, a covariate shift, and predictions moving within
their bins with calibration unchanged): the alarm rate is at most 4.0% and the flag rate (warn
or alarm) at most 4.7% in every one of the 120 cells. With true drift (100 checks per cell)
an overconfident model (predictions 0.6 times as far from 0.5 as the truth) alarms in 63% to
100% of checks once the new batch has 200 rows (85% to 100% with a reference of 1,000 rows or
more); a +0.10 bias alarms in 37% of checks at 1,000 rows each, 72% to 83% when one side has
5,000 and 100% at 5,000 each, but seldom against a 200-row reference (at most 4%); a model 0.2
to 0.3 off still alarms in a 100- or 300-row batch. The power is as before the gap change.

A change is `changed` when its Holm-adjusted p-value (across the calibration block) is below
0.05; `warn` when it changed for the worse (Brier or ECE up, a bin's |observed − mean predicted|
gap up); `alarm` when, in addition, its whole interval lies beyond the tolerance. The tolerances
are Brier 0.01, ECE 0.02 and bin 0.10 (OWNER D-73), each overridable. A change without an
interval is `no_interval` and never flagged.

**Bins must align to be compared.** Aggregated reliability bins are compared only when the
drift config declares `binning: equal-width`. Quantile bins (as respectful-offers exports
today) move with each period, so per-bin drift is reported as `not available`
(`bins_not_aligned`) and only Brier and ECE are compared.

**A bin on one side only is not comparable.** An export that leaves empty bins out (an absent
top bin `[0.9, 1.0)` is common) gives a reliability bin on one side only, or on neither. A
bin on neither side plays no part. A bin on one side only gets no bin delta and is listed in
`bins_not_comparable` (`bin`, `present_in`: `reference` or `new batch`); its rows still count
in Brier and ECE. It is never an error and never a flag by itself.

## The drift config

```yaml
psi_warn: 0.1
psi_alarm: 0.25
features:
  amount: {psi_warn: 0.05, psi_alarm: 0.2}
calibration:
  brier_tolerance: 0.01
  ece_tolerance: 0.02
  bin_tolerance: 0.10
  binning: equal-width   # equal-width, quantile or unknown (aggregates only)
severity:                # each warn or fail; default fail
  psi: fail
  calibration: warn      # Brier and ECE
  bins: warn             # the reliability bins
```

Unknown keys are refused. **Severity** works as in [gates](gates.md): an alarm in a check set
to `warn` is still reported (the verdict, the `alarms` count, the row's level or status, each
row's `severity`) but never sets exit 1; `blocking_alarms` counts the alarms that do. A
project whose owner rules that drift only warns (respectful-offers, its OWNER D-56) sets all
three to `warn`.

## Outputs and exit codes

`drift check` prints a summary and, with `--out`, writes `drift-1` JSON: the reference and new
batch hashes, the settings, one row per feature (`psi`, `psi_shown`, `level`, its levels,
`bins`, `n_ref`, `n_new`, `expected_psi`, `small_batch`, `missing_share_ref`,
`missing_share_new`, `severity`, and for pre-binned features `bins_one_side`) and the
calibration block (`deltas`, each with its `severity`; `bins`, `bins_not_comparable`,
`tolerances`, `level`), with the `alarms`, `blocking_alarms` and `warns` counts. There
is no timestamp: the same inputs give the same bytes.

`--emit-metrics FILE --project NAME [--run-id ID] [--prefix drift]` also writes a metrics-1 file
(`drift.psi` per feature, the new batch's `drift.brier` and `drift.ece`, and
`drift.brier_delta` / `drift.ece_delta` with their intervals), so the figures can be gated
with `evalkit gate` and added to the run history, where they get alarm bands once five periods
are in and appear in the trend page's PSI panel.

Exit codes: 0 no alarm, or alarms only in checks at `warn` severity (printed), 1 an alarm in a
check at `fail` severity (the default: PSI, calibration or bins), 2 bad input (an edited
reference, an empty batch, a missing feature or column, pre-binned sides with no bin in
common, a bad severity).

With `--manifest heldout/MANIFEST.json`, a reference or report that would name a held-out case
id (for example a categorical feature of case ids) is refused (exit 2) and nothing is written.

## From Python

```python
from evalkit import drift

ref = drift.freeze_reference("alerts-2026-10.jsonl", features=["amount", "risk_score"])
report = drift.check(ref, "alerts-2026-11.jsonl")
print(report.exit_code, [(f["feature"], f["psi_shown"], f["level"]) for f in report.features])
print(drift.psi([1.0, 2.0, 3.0, 4.0], [2.0, 3.0, 5.0, 8.0]), drift.psi_level(0.12))
```
