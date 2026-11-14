# A2.5 — Aggregate and CIs

**Wave 2. Depends on:** A2.2, A2.4. **Unblocks:** A2.6, B2.1, C3.4.

## Goal
Turn scored records into `summary.json`: one headline per model and judge (label-correct AND fully grounded), every figure with a bootstrap 95% CI, paired model comparisons with a Holm-corrected tie rule (OWNER D-36), thresholds, error accounting, the self-preference table, and honest `judge_mode` / `calibration_status` / `judging_status` stamps.

## Outputs (`evalkit/pipeline/aggregate.py`, `evalkit/stats.py`)
- `score_record(record, claim_set_coverage, judges)` → `Scores`. A record whose completion has an error is incorrect, has groundedness 0, is not fully grounded, and is not correct-and-grounded for every judge (AGG-05, NFR5). Coverage comes from the extractor (D-31).
- `stats.Resamples(n_cases, n_resamples=10_000, seed)`: one seeded index matrix over the run's case ids, reused for every model and metric (D-37). `interval(values, resamples)` → `Interval` (percentile, 95%). Pure Python.
- `stats.paired(values_a, values_b, resamples)` → difference `Interval` and a two-sided bootstrap p-value. `holm(pvalues, alpha=0.05)` → which pairs are separated (D-36).
- `build_records(generated, claim_sets, judging, ...)` → scored `Record`s; `aggregate(records, *, selection, judging, agreements, ...)` → `RunSummary` with a `ModelSummary` per model (AGG-01, AGG-02): headline and mean groundedness per judge, accuracy, citation validity, coverage, cost per 100 cases, cost per correct answer, latency p50/p95, `n_errors` by type, thresholds.
- Records are aligned by case id: every model must answer the same case set, or `aggregate` refuses (paired comparisons need shared cases).
- `RunSummary.comparisons`: for each judge and each pair of models, the headline difference with its CI, p-value and `separated`, Holm-corrected within each metric and judge (AGG-03). Accuracy comparisons are judge-free and listed once.
- Thresholds: `metric op value` evaluated on point estimates; `failed_thresholds` lists the failures as text (AGG-04). An unknown metric name is a config error, raised before any model call (validated in `RunConfig`).
- Self-preference: judge × generator family → mean groundedness `Interval` (AGG-06). Empty in single mode.
- `write_summary(summary, path)`: atomic write of `summary.json`; `judging_status="pending"` while any judge has open batches, with empty per-judge maps (D-37) (AGG-07).
- Stamps are derived, never passed in: `judge_mode` from the `JudgeSelection`, `judging_status` from the `JudgingResult`s, `calibration_status` from whether held-out `JudgeAgreement` exists for every judge (A2.6 supplies it); `family_for(model)` from the provider; `stamp_lines(summary)` gives the exact caveat text that every chart and report must show (AGG-08).

## Rules
- Denominator is every case each model was asked, never only successful calls (NFR5).
- No metric is reported without an interval except cost and latency (the run's measured totals).
- The bootstrap is seeded from `RunConfig.seed`: the same records give byte-identical `summary.json` (repro).
- A golden run (`tests/golden`) drives generate → validate → extract → judge → score → aggregate on the 12-case fixture with scripted models and must match `tests/fixtures/golden/summary_pubmedqa_mini.json` byte for byte, rerunning entirely from cache.
- Honesty rules (D-39): latency over calls that reached the provider, null when none did; a model whose calls all failed fails every threshold; calibration requires agreement scored on this task under the current `judge_prompt_hash`; stale, foreign or misaligned inputs are refused.
