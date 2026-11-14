# A2.6 acceptance criteria

**CAL-01** `export_sample` returns 100 answers stratified by model and reference label (each stratum within one answer of proportional), 50 tuning and 50 held-out with no overlap, and 20 relabel answers; the same seed gives the same sample.
**CAL-02** The labelling page and CSV show claim text and cited sentences but no verdicts or model names; the page binds to 127.0.0.1 only, saves every label atomically, and the relabel round hides first-pass labels; `import_labels` round-trips a filled sheet and rejects unknown claim ids, blank or invalid labels, and edited claim text.
**CAL-03** Kappa, raw agreement and the confusion matrix match hand-computed values on a fixture; the kappa CI is a cluster bootstrap over answers; kappa per family is reported for each family present.
**CAL-04** Extraction error rate excludes nothing silently and matches the fixture; self-consistency kappa compares the relabel against the first pass on the same claims only.
**CAL-05** A report carries `judge_prompt_hash`; `calibration_status_of` returns calibrated only when every judge's agreement was scored under the current hash.
**CAL-06** A regression test fails when held-out kappa falls by more than 0.05 against the recorded baseline and passes otherwise.
**CAL-07** `publish_guard` refuses single-judge or uncalibrated summaries with a message naming the missing condition.
**CAL-08** `docs/calibrate-your-task.md` exists, is linked from the uncalibrated stamp, and walks through export, labelling, import and scoring.
