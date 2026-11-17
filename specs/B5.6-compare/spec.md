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
