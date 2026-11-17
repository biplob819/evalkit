# B5.6 acceptance criteria

**RVR-01** `evalkit compare <old> <new>` and a public function: per metric and label, the change and a 95% interval; paired two-level bootstrap (cases, then trials) on shared case ids, unpaired otherwise and labelled so [D-62]
**RVR-02** Changes whose interval excludes 0 are flagged after Holm adjustment; differing snapshots, code versions and config hashes are stated; JSON and a markdown table; held-out aggregates only [D-62]
**RVR-03** Baselines that never skip silently: a baseline names every gated metric with at least 2 shared cases; recorded settings (rules, bar, models, config, assessments hashes) that differ are listed; a case-only resample when there is no trial level (pass^k over cases); the per-case sidecar is optional and held-out rows fall back to unpaired (course-assistant, assay-pipeline) [D-65]
