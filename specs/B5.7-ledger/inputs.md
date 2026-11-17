# B5.7 inputs — what each asking project uses today

Read-only survey of 2026-11-17 by the lead (spec written ahead). Aggregate shapes only: field names, types, units, file paths and commits. No data rows, no case or member ids, no held-out content, no secrets; no `.env` file was read. Commits are the `main` ref of each local clone on that date (read from the ref file, not fetched). Line numbers are approximate.

## EvalKit itself (this repo, base commit 864a801)

| What | Where | Shape |
|---|---|---|
| metrics-1 (B5.3) | `src/evalkit/metrics.py` | top level `schema_version` (`"metrics-1"`), `project`, `run_id`, `created_at` (ISO with zone), `simulated` (bool), `provenance {code_version, snapshot, config_hash, evalkit}`, `metrics[]`; each metric `name` (`[a-z][a-z0-9_.]*`), `unit` (`count`, `rate`, `bool`, `ratio`, `ms`, `s` or an upper-case currency code), `value`, `n`, `k` (rates only, value = k/n within 1e-6), `ci_low`/`ci_high` (must contain `value`) with `ci_method`, `labels` (no id-like keys). Unique (name, labels). |
| Efficiency names (EFF-01) | `metrics.py` `EFFICIENCY_NAMES` | `tokens_{in,out,reasoning,cached}_per_case` (count means), `cache_hit_share` (rate), `cost_per_case`, `cost_per_correct`, `eval_overhead_cost`, `system_cost` (currency), `latency_p50/p95/p99` (ms or s), `retry_rate`, `error_rate` (rate); label `mode` ∈ {batch, live} |
| summary.json `efficiency` (EFF-02, D-70) | `src/evalkit/schemas/summary.py` `Efficiency` | per model: `mode`, `price_date` (YYYY-MM-DD or null), `n`, token means, `cache_hit_share`, `cost_per_case_usd` (Interval), `cost_per_correct_usd` (Interval or null), `n_correct`, `latency_p50/p95/p99_ms` (Interval or null), `n_latency`, `error_rate`, `n_errored`, `retry_rate`/`n_retried` (null today), `eval_overhead_cost_usd`, `system_cost_usd` |
| Gates (B5.3) | `src/evalkit/gates.py` | `Gate {metric, labels ("*" = any), op (>=, <=, ==), value, bound (point, lower, upper), severity (fail, warn), disparity}`, `GatesFile {gates, heldout_splits}`; exits `EXIT_PASS 0, EXIT_FAIL 1, EXIT_INPUT 2, EXIT_COST_CAP 3, EXIT_PENDING 4`, precedence 2 > 3 > 4 > 1 > 0 |
| Compare (B5.6) | `src/evalkit/compare.py` | `compare(old, new, *, gates, manifest, heldout_splits, seed=7, resamples=10_000) -> Comparison`; `report_json` is `compare-1` (sorted keys, no timestamps); exits 0 / 1 (fail-gate regression) / 2; held-out rows folded to (metric, split, model, judge); settings block lists differing `config_hash`, `code_version`, `prompt_hash`, `model_ids.*`, `settings.json` keys |
| Held-out log (B5.14) | `src/evalkit/heldout.py` | canonical JSON lines (sorted keys, compact, ASCII, LF) with `prev` = sha256 of the previous line (`GENESIS` = 64 zeros); `append_entry` under an exclusive file lock; `heldout_scored {rubric_hash, case_id_sha256[], n_cases, at, note?}` refused a second time per (rubric, case); rubric hash `[0-9a-z][0-9a-z._:+-]{0,127}`, prefix `calibration:` reserved for B5.9; `chain_problems`, `append_only_problems`, `ci_revisions`; manifest `evalkit.heldout.manifest/1` with `splits {split: [{id_sha256, content_sha256}]}` and `manifest_sha256` |
| Spend ledger | `docs/measurements/spend-ledger.jsonl` (22 lines), written by `src/evalkit/smoke.py` `record_spend(kind, spent_usd, detail, ledger)` | JSONL, `json.dumps(sort_keys=True)`, not hash-chained, no lock: `at` (ISO with zone), `kind` (str), `spent_usd` (float, 6 dp), optional `run_id`, `n_cases`, `judges` (list), `note`; returns the running total. No cap or alarm. Caps elsewhere: NFR2 $20 per 200-case run, demo $3 a day. |
| Report charts | `src/evalkit/report/svg.py` | `Point`, `nice_ticks`, `log_ticks`, `money`, `scatter`; reports are deterministic HTML |

## respectful-offers — `/Users/biplob.chakraborty/Respectful Offers`, commit 619e437110e1

Asks for: LED-01..04 (churn calibration block, PSI per feature). Decisions: OWNER D-50 (`offers monitor`/`challenge` built on EvalKit B5.7), D-51 (LED-04 admitted for B5.7, v0.4.0), OWNER D-52 (gates `churn.auc` lower bound >= 0.80 warn, `churn.ece` <= 0.05 warn). Plan: `specs/E4.3-monitor-challenge/spec.md` (EvalKit computes PSI and the deltas; the repo adds no PSI code; challengers go to the LED-03 experiment ledger, whose freeze guard stops tuning on the judged period).

- Calibration code: `src/respectful_offers/churn/model.py`. `N_BINS = 10`, `N_BOOT = 1_000`, risk clipped to [0.001, 0.999].
  - `ece(y, p, bins=10)`: 10 **equal-width** bins, index `floor(p*bins + 1e-12)` clipped (p = 1 in the last bin), empty bins skipped, no interval.
  - `reliability(y, p, bins=10) -> list[(mean_pred, obs_rate, n)]`: 10 **quantile** bins (`np.array_split` of a stable argsort), edges not stored, not from a reference. Bins therefore do not align across periods.
  - Brier: `sklearn` `brier_score_loss`; interval a member-level bootstrap, 1,000 resamples, raw 2.5/97.5 percentiles, **not clamped to contain the point** (a metrics-1 interval must contain its value, so such a row would be refused by `evalkit gate`).
- Output: `ChurnReport` (`src/respectful_offers/schemas/summary.py`): `auc`, `brier`, `baseline_auc` as `{point, lo, hi}`; `ece` float; `reliability` positional `[(mean_pred, obs_rate, n)]`; `features [str]`. Written to `results/kkbox/churn/report.json` and embedded as `churn` in `results/kkbox/summary.json`.
- metrics-1 exporter `src/respectful_offers/metrics1.py` (`to_metrics1`, writes `results/kkbox/metrics.json`): `churn.auc`, `churn.baseline_auc`, `churn.brier` (unit `ratio`, with CI, `ci_method` "block-bootstrap percentile 95% (D-16)"), `churn.ece` (`ratio`, no CI), `churn.reliability.predicted` and `churn.reliability.observed` (`rate`, labels `{bin: int}`, `n` = bin count).
- Planned drift input (E4.3 spec line 19): `drift.feature_share` (rate) labels `{feature, bin}`, fixed bin edges stored with the baseline, bins under 30 members merged into a neighbour. EvalKit receives **pre-binned shares, never rows**.
- Run records: `runs/<name>/run.json` `{config_hash, git_sha, run_id, seed, simulator_hash, stages[{stage, key, parents, files, started_at, seconds, cache_hit, error}]}`. No JSONL ledger, no PSI code.

## fraud-review-bedrock — `/Users/biplob.chakraborty/Field PM/fraud-review-bedrock`, commit ca8e895d200e (EvalKit pin `add5af9b8af3`)

Asks for: LED-01..05. Decisions: D-36 (monitoring: run history, alarm bands, a drift check, a trend panel, and a logged improvement loop on the dev set), D-38 (monitoring moves into EvalKit). The PSI defaults are its Tech Spec MON-03 (`TECH_SPEC.md` line 858: "per agent-visible feature between the frozen eval alerts and a new batch; warn above 0.1, alarm above 0.25", traced to D-36, D-38); MON-05 (line 860): EvalKit B5.7 replaces its local history, bands, PSI and trend; IMP-01 (line 861): experiment ledger.

- PSI: `src/fraudreview/monitor.py`, `PSI_EPSILON = 1e-4`, `PSI_WARN = 0.1`, `PSI_ALARM = 0.25`, 7 numeric `DRIFT_FEATURES`.
  - `psi(reference, current, bins=10)`: edges = `np.unique(np.quantile(ref, linspace(0, 1, bins+1))[1:-1])` (reference deciles, linear interpolation, duplicates merged), assignment `searchsorted(edges, x, side="right")` (bins `[e_i, e_{i+1})`, open ends); constant reference gets 3 bins; null and non-finite values dropped; empty sample raises; both shares floored at 1e-4 (not renormalised); PSI = Σ (c − r)·ln(c / r).
  - `drift_level`: `ok` below 0.1, `warn` for 0.1 ≤ x ≤ 0.25, `alarm` above 0.25. `drift(reference_cases, new_cases) -> {feature: (psi rounded to 4 dp, level)}`. Not persisted; no CLI or exit code.
  - Reference batch: no stored profile; `scripts/monitor_sample.py` reads `results/baseline/cases_eval.jsonl` (reference) and `cases_eval2.jsonl` (new); identity only `cases_sha256` per run and the `docs/measurements/freeze.json` digest.
- Bands: `check_bands` returns `Alarm(metric, level warn|fail, value, band)`; fixed bands in `config.yaml` (`warn_above`, `fail_above`, `warn_below`, `fail_below`); leakage above 0 always fails. No Brier, ECE or reliability code.
- Run history `docs/measurements/runs.jsonl` (`run_history_line`, sorted-key JSONL, one O_APPEND write, not chained): `run_id`, `digest`, `source` (str); `n`, `frauds` (int); `leakage {k, n, point, hi}`; `auto_clear_rate`, `auto_clear_precision {k, n, point, lo, hi}`; `cost_per_1000_usd` (float or null), `cost_basis` (str); `schema_error_rate`, `refusal_rate` (float); `latency_p95_stage2_ms` (int or null).
- Experiment ledger `docs/measurements/experiments.jsonl` (`experiment_line`): `hypothesis`, `change`, `digest_before`, `digest_after` (str); `dev_before`/`dev_after {run_id, n, frauds, leakage{k, n, hi}, auto_clear_rate{k, n, point}, decision_cost_usd}`; `auto_clear_change` (float); `leaks_before_after [int, int]`; `paired` (object or null, with `survives_holm`); `rule {verdict, reason}`, verdict `keep|revert`; `note`. `trend()` returns `[{metric, run_ids, values}]`.

## course-assistant — `/Users/biplob.chakraborty/AI teaching Assistant`, commit 9e98f51f8aef

Asks for: LED-06 (spend ledger with a cap alarm) and release-gate rigour. Decisions: OWNER D-37 (cap $150 a month, 80% alarm), D-64 (alarm moves to $135, 0.9), D-55 (hash-chained tuning log, append-only in CI from `LOG_BASE_SHA`), D-53 (run cap split and reserved before spending), D-16 (demo daily spend counter), D-68 (promote release gate, tuning log, held-out discipline and spend ledger with an alarm into EvalKit).

- Spend ledger `runs/ledger.jsonl`, `src/course_assistant/llm/ledger.py`, `Ledger(path, monthly_budget_usd)`, `.append(*, purpose, model, tokens_in, tokens_cached, tokens_out, file_search_calls, cost_usd, run_id="", at=None, tokens_cache_write=0, tokens_reasoning=0, priced=True, lock_timeout_s=5.0)`. One line **per priced call**, sorted keys: `at` (ISO UTC, seconds), `purpose` (`[a-z_]{1,32}`), `model`, `tokens_in`, `tokens_cached`, `tokens_out`, `file_search_calls`, `tokens_cache_write`, `tokens_reasoning` (int), `cost_usd` (float, 8 dp), `run_id` (`[A-Za-z0-9._-]{0,64}`), `priced` (bool). Append under an `fcntl` lock; not chained.
- Cap: `monthly_budget_usd` (config `schemas/config.py`; template 50.0, scripts 150.0), cumulative per UTC calendar month (filter on `at` prefix `YYYY-MM`). `WARN_AT = 0.9`: when a write crosses 0.9 × budget a warning is stored once; nothing reads it, no exit code, no hard stop at the cap. Hard per-run stops: `RunBudget` reserve/settle (`eval/judges.py`), `EXIT_COST_CAP = 3` (`eval/release.py`).
- Release gates (`eval/release.py`): `LATENCY_P95_MS = 11_000`, `MAX_CASE_COST_USD = 100.0`, a `cost_per_1000_usd` monitor; exits 0/1/2/3/4. No cross-run history store (`.evalkit/store` is a cache).
- Tuning log `docs/measurements/tuning-log.jsonl` (`eval/tuning.py`): `prev` chain from `GENESIS = "0"*64`; kinds `grounding_bar`/`support_rules`, `heldout_scored {run, bar, ground_version, at}`, `calibration_scored`; `append_only_problems` in CI. (The model for B5.14.)

## assay-pipeline — `/Users/biplob.chakraborty/Trusted Assay pipeline`, commit 6e3a1405ba7c

Second user of the freeze guard and a spend-ledger reader. Decisions: OWNER D-12 (the held-out set "runs once, before publishing"; no look log exists), OWNER D-41 (adopts gates and compare, exports metrics-1), OWNER D-42 (efficiency lane). Nothing on B5.7 yet.

- Runs: one folder per run `runs/<name>-<date>/` with `run.json {db_sha256, snapshot, seed, cases (digest), k, models}` and `summary.json {estimate_usd, spent_usd, judging_spent_usd, k, snapshot, trials_sha256, ...}`; metrics-1 at `docs/results/<run>/metrics.json`; `docs/results/LATEST` pointer. No history index.
- Spend ledger `docs/measurements/spend-ledger.jsonl`, written by hand: `date` (YYYY-MM-DD), `node`, `run`, `model` (str), `calls` (int), `usd` (float). Rule (Tech Spec): every real run appends a line; over about $25 needs owner approval. Per-run `--max-cost` stop; console `DAILY_CAP_USD = 3.0`.
- Held-out: `heldout/MANIFEST.json {files (sha256 per file), attacks_per_category, questions_per_kind}`, checked by `verify_heldout`.

## agentic-analytics — `/Users/biplob.chakraborty/agentic-analytics`, commit a638db93c74c (the copy under `Agentic Analytics/` is at the same commit)

Adopts B5.7 for run trend and drift (OWNER D-74: run history and a trend page with drift flags; OWNER D-75: from EvalKit's B5.6 compare and B5.7 ledger instead of a local page). D-78, D-85: spend-ledger line rules; OWNER D-80: $25 cap for the baseline.

- Runs: `results/<kind>-<ts>-<hash>/run.json {run_id, kind, config_hash, git_sha, semantic_hash, fix_hash, snapshot_sha256, ..., seed, n_resamples, exit_code, spent_usd, cumulative_usd, ...}`, `summary.json {run_id, config_hash, snapshot_sha256, total_cost_usd, ..., cost_status}`; `results/latest` pointer. Floors: `monitoring/thresholds.json {schema_version, note, min_accuracy {backend: float}}`, exit 1 below a floor.
- Spend ledger `docs/measurements/spend-ledger.jsonl` (`append_ledger`, `src/agentic_analytics/cost.py`): `at` (ISO), `kind`, `run_id`, `spent_usd` (float), `credits {str: float}`, `note` — the same family as EvalKit's. `SpendCap(cap_usd)` reserve/settle, `CostCapReached` → exit 3.
- Freeze: `fixes/FROZEN.json {kind: "fix_freeze", pack, fix_hash, files, blocks (sha256), frozen_at, reason}`; re-freeze needs `force` and a reason. It freezes the candidate (the fixes), not a held-out split; `fix_hash` is the natural `candidate_sha256` for LED-03.

## What this means for B5.7

1. PSI must accept **pre-binned shares** (respectful-offers) and **raw values** binned by fraud-review-bedrock's exact rule, with its 1e-4 floor, so its numbers reproduce (D-61 rule 3).
2. Reliability-bin deltas need bins that align across periods: equal-width bins do, respectful-offers' quantile bins do not (open question 3 in spec.md).
3. Three spend-ledger shapes exist (EvalKit/agentic-analytics `at, kind, spent_usd`; course-assistant per call `at, cost_usd`; assay `date, usd`), so the cap check reads any of them through a field map; only EvalKit's writer chains lines.
4. The default ledger paths (`docs/measurements/runs.jsonl`, `experiments.jsonl`, `spend-ledger.jsonl`) match what fraud-review-bedrock, assay-pipeline and agentic-analytics already use; their existing unchained lines are migrated, if at all, by their own builds (D-61 rule 4).
5. No project has a held-out look log; course-assistant's tuning log and B5.14 are the mechanism the freeze guard reuses.
