# A2.6 — Calibration

**Wave 2. Depends on:** A2.4, A2.5. **Unblocks:** B2.1, B2.2.

## Goal
Measure how far each judge can be trusted on this task, against the owner's hand labels: a stratified 100-answer sample split into 50 tuning and 50 held-out answers plus a blind 20-answer relabel, kappa with bootstrap CIs per judge and per generator family, the extraction error rate, and the owner's self-consistency. Calibration is tied to the exact rubric (`judge_prompt_hash`), so a rubric change can never keep an old "calibrated" stamp.

## Outputs (`evalkit/pipeline/calibrate.py`)
- `export_sample(records, seed)` → 100 answers stratified by model and reference label, split 50 tuning / 50 held-out with a fixed seed, plus 20 answers drawn for relabelling (CAL-01).
- Labelling page (OWNER D-40): `serve_labeller(sheet, labels_path)` runs a stdlib HTTP server on 127.0.0.1 showing one claim at a time with its cited sentences, keys S / U / E (extraction error) / Backspace (back), autosaving each label atomically; it renders only claim text and cited sentences. The relabel round shows the 20 relabel answers in a fresh order without the first-pass labels.
- Labelling sheet: a CSV with one row per claim (answer id, split, claim text, cited sentences shown in full, empty `label` and `extraction_error` columns), written without any judge verdicts or model names (blind, B4). `import_labels(path)` validates every row into `CalibrationLabel`; unknown claims, missing labels or a changed claim text are errors (CAL-02).
- `agreement(labels, verdicts, judge, family_of)` → `JudgeAgreement`: Cohen's kappa with a bootstrap 95% CI over answers (clustered, because claims in one answer are not independent), raw agreement, the 2×2 confusion matrix, and kappa per generator family (CAL-03).
- Extraction error rate (share of claims flagged `extraction_error`) and the owner's self-consistency kappa (relabel vs first pass) (CAL-04).
- `CalibrationReport` stamped with `judge_prompt_hash` and the held-out scoring time. Scoring the held-out split records the hash; a later rubric change makes `calibration_status` `uncalibrated` until a fresh held-out set is labelled (CAL-05).
- Regression fixture: the held-out labels plus recorded verdicts; a CI test fails if held-out kappa drops by more than 0.05 for a rubric or judge-model change (CAL-06).
- `publish_guard(summary)` refuses unless `judge_mode == "dual"` and `calibration_status == "calibrated"` (B8, CAL-07).
- `docs/calibrate-your-task.md`: the step-by-step guide that the "uncalibrated" stamp links to (CAL-08).

## Rules
- Rubric tuning reads only the tuning split; the held-out split is scored once per rubric hash.
- Claims flagged `extraction_error` are excluded from kappa and reported in the extraction error rate.
- The owner labels; nothing in this node calls a model.
- Integrity rules (D-43): keyed, content-bound sheet keys and shuffled answer order; label files bound to sheet and round; 95% judge coverage to certify; only held-out agreement certifies; a held-out set cannot be re-scored under a new rubric; the regression fixture records the rubric hash and judge snapshots; the page accepts only same-origin JSON posts.
