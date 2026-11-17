# B5.6 — Run-vs-run compare

**Wave 5. Depends on:** B5.3 (metrics-1, gates, one-sided/two-sided Clopper-Pearson). **Release:** v0.3.0. **Asked by:** assay-pipeline (D-62), fraud-review-bedrock (drift, D-62), course-assistant (baselines, D-65), the portfolio's before/after view. Decisions: D-62, D-65, D-66, D-70.

## Goal
Say, per metric and label, whether a new run is better or worse than an old one, with an honest 95% interval and without leaking held-out cases.

## Interface (the lane builder completes exact names before tests)
- `evalkit compare <old> <new> [--json] [--out compare.json] [--md compare.md]` and a public function (e.g. `evalkit.compare.compare_runs(old, new, *, seed=7, resamples=10_000) -> Comparison`). Inputs: EvalKit run folders (summary.json + records.jsonl) or metrics-1 files, each optionally with a per-case sidecar `cases.jsonl` (`case_id`, `trial` (optional), `metric`, `value`, `labels`).
- **RVR-01:** per metric and label (model × category × split …), the change new − old and a 95% interval: a PAIRED two-level bootstrap over shared case ids (cases, then trials within a case, as D-41/D-51) when both runs carry per-case values for the same ids; a case-only resample when there is no trial level (pass^k over cases); otherwise UNPAIRED (difference of independent bootstrap or supplied intervals) and labelled `unpaired` in every output.
- **RVR-02:** changes whose interval excludes 0 are flagged after Holm adjustment across the compared metrics of one comparison; differences in snapshot, code version, config hash, prompt/template hash, model ids and EvalKit version between the two runs are stated at the top (a comparison across different settings is labelled, never silently mixed); JSON plus a short markdown table; held-out splits (`heldout_splits`, or B5.14 manifest membership) appear only as split-level aggregates, never case ids.
- **RVR-03:** a baseline must name every gated metric with at least 2 shared cases (course-assistant); the comparison never skips a metric silently — missing metrics/labels on either side are listed; recorded settings that differ (rules hash, bar, models, config hash, assessments hash) are listed; the per-case sidecar is optional and held-out rows in it carry no case_id, so they fall back to unpaired (assay-pipeline).

## Tests from the asking projects
- assay-pipeline: two runs where one model gains one attack success; pass^k compared over cases only; the held-out split shown as aggregates; sidecar with public cases only.
- course-assistant: a baseline missing a gated metric is an error (exit 2), a metric with fewer than 2 shared cases is an error, settings differences listed.
- A paired comparison checked against an independent paired bootstrap; an unpaired one labelled.

## Out of scope
History and trends across many runs (B5.7); computing metrics from raw outputs.

## Exact interface (lane builder, before tests)

Module `evalkit.compare` (public since 0.3.0; its `__all__` is the contract):

- `load_input(path) -> RunInput`: `path` is an EvalKit run folder (or its `summary.json`), a folder holding `metrics.json`, or a metrics-1 file. A metrics-1 input reads an optional per-case sidecar `cases.jsonl` and an optional `settings.json` from the same folder; an EvalKit run folder derives per-case values from `records.jsonl` (its own `cases.jsonl` holds case definitions and is never read as a sidecar) and may also carry `settings.json`. Raises `CompareError` (messages never echo values or case ids).
- `compare(old: RunInput, new: RunInput, *, gates: GatesFile | None = None, manifest: Mapping | None = None, heldout_splits: Sequence[str | int | bool] = (), seed: int = 7, resamples: int = 10_000) -> Comparison`.
- `compare_runs(old, new, *, gates=None, manifest=None, heldout_splits=(), seed=7, resamples=10_000) -> Comparison`: paths (gates and manifest as paths or loaded objects); never raises for bad input, returns a `Comparison` with exit code 2 and its errors.
- `paired_bootstrap(old: Mapping[str, Sequence[float]], new: Mapping[str, Sequence[float]], *, seed=7, resamples=10_000, key="") -> tuple[Interval, float, str]`: mean(new − old) over shared case ids with its 95% percentile interval, a two-sided bootstrap p-value (+1 correction) and the method. Two-level (cases, then each side's trials within a case) when any shared case has more than one value; otherwise a case-only resample that uses `evalkit.stats.Resamples` / `paired` exactly.
- `render_markdown(c) -> str`, `report_json(c) -> str` (sorted keys, indent 2, LF; no timestamps, so the bytes repeat).
- Data: `CaseValue(metric, labels, value, case_id=None, trial=None)`, `RunInput(path, kind, metrics, cases, settings, status)`, `Change`, `Missing`, `SettingDifference`, `Comparison` (each with `as_dict()`), `CompareError`, `COMPARE_SCHEMA = "compare-1"`, `EXIT_PASS = 0`, `EXIT_REGRESSION = 1`, `EXIT_INPUT = 2`.

Sidecar `cases.jsonl`, one JSON object per line: `metric` (metrics-1 name), `value` (number or true/false), optional `labels` (as metrics-1, no id keys), optional `case_id` (non-blank string; held-out rows leave it out), optional `trial` (integer ≥ 0). Unknown keys, duplicates of (case_id, trial, metric, labels), a case with and without `trial` for one metric, and a row whose (metric, labels) is not in the metrics file are refused (exit 2).

Statistics per (metric, labels) present on both sides:
1. **paired** when both sides carry per-case values with case ids, the row is not held-out and at least 2 case ids are shared: `paired_bootstrap` over the shared ids (rows outside the shared set, including id-less rows, are counted in `not_paired`).
2. otherwise **unpaired**: each side's 95% interval — per-case bootstrap (two-level when trials exist) if that side has per-case values, else the supplied `ci_low`/`ci_high`, else two-sided Clopper-Pearson from `k`/`n` — combined by MOVER (Zou & Donner 2008) into an interval for new − old; the p-value is the normal approximation from the half-width on the side of zero. No interval on either side: change shown, no interval, never flagged. Labelled `unpaired` in JSON and markdown.
3. Flags: Holm (`evalkit.stats.holm`, alpha 0.05) across every compared row with a p-value; a row is `flagged` when Holm rejects and its interval excludes 0. Adjusted p-values are shown.

Held-out: a row is held-out when its `split` label (canonical string) is in `heldout_splits` ∪ the gates file's `heldout_splits` ∪ {`heldout`}; held-out rows are always unpaired and are printed only folded to split level (metric, split, model, judge; row count, flagged count, regression). With a B5.14 `manifest`, per-case values whose case id falls in a held-out split of the manifest lose their case id on load (never paired). No output ever prints a case id.

Gates and exit codes (`--gates`): a row is gated when a gate matches it (metric, labels with `*`, disparity label present). A **regression** is a flagged row matched by a non-disparity gate whose change moves toward failing it (`>=`: new − old < 0; `<=`: > 0; `==`: any flagged change). Exit **1** when a regression is matched by a `severity: fail` gate; warn-gate regressions are listed and exit 0. Exit **2** (precedence over 1): unreadable or invalid input, a unit that differs between the runs, and the RVR-03 baseline rules: every gate must match a row in old (the baseline) and in new, every matched row must exist on both sides, and a matched public row with per-case values on both sides needs ≥ 2 shared case ids; a matched row compared from summaries or held-out needs n ≥ 2 on each side when n is known. Without `--gates` the exit code is 0 or 2.

Settings block: `project`, `simulated`, `snapshot`, `code_version`, `config_hash`, `evalkit`, `models` (distinct `model` labels or run.json models), and for run folders `prompt_hash`, `model_ids` (provider snapshots), `judges`, `judge_mode`, `judge_prompt_hash`, `extractor_version`, `results_format`, `seed`; plus every key of an optional flat `settings.json` (e.g. `rules_hash`, `bar`, `assessments_hash`). Every setting recorded differently (or on one side only) is listed; `settings.status` is `different`, `partly unrecorded` or `same`.

CLI: `evalkit compare OLD NEW [--gates FILE] [--manifest FILE] [--heldout-split NAME]... [--seed 7] [--resamples 10000] [--json] [--out compare.json] [--md FILE]`; compare.json is written on every exit path.
