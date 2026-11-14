# A2.5 acceptance criteria

**AGG-01** For each model and judge, `headline` is the share of all cases that are label-correct and fully grounded by that judge; it matches a hand-computed fixture exactly.
**AGG-02** Every rate in `ModelSummary` carries a percentile bootstrap 95% CI from 10,000 seeded resamples over cases; the interval covers the true rate at close to the nominal rate in a simulation, and the same seed gives identical output.
**AGG-03** For every pair of models (per judge for the headline, once for accuracy) the summary holds the paired difference CI, a two-sided bootstrap p-value and `separated`, decided by Holm at 5% across all pairs in the run (D-36); identical models are never separated and a large true gap is.
**AGG-04** Thresholds evaluate as `metric op value` on point estimates; `passes_threshold` is true only if all hold; `failed_thresholds` names each failure; an unknown metric is rejected when the config loads.
**AGG-05** A failed call counts in the denominator as incorrect and ungrounded for every judge, and `n_errors` reports it by type; dropping errors would change the fixture's headline, and the test proves it does not happen.
**AGG-06** In dual mode the self-preference table holds mean groundedness per judge × generator family with CIs; in single mode it is empty.
**AGG-07** `write_summary` writes `summary.json` atomically; while a judge is pending it records `judging_status="pending"` with deterministic metrics and empty per-judge maps, and no `finished_at`.
**AGG-08** `judge_mode` and `calibration_status` are stamped from their sources (never hard-coded), and `stamp_lines` returns the caveat text for single-judge and uncalibrated runs.
