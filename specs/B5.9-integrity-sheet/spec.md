# B5.9 — Calibration integrity sheet

**Wave 5. Depends on:** A2.6, B5.2 (D-66). Decisions: D-62, D-65, D-69. Release: v0.4.0.

Skeleton by the lead; the lane builder completes Goal, Why, Interface and Out of scope before tests. Requirements from course-assistant are in `inputs.md`.

## Capabilities
- **CIS-01**: Integrity sheet: blind repeats and planted items mixed into a second labelling pass through the labelling page [D-62]
- **CIS-02**: Labeller self-agreement and planted accuracy reported with CIs; calibration is not certified below the bar; the certificate is bound to the run and rubric hashes [D-62]
- **CIS-03**: Calibration sheets for any criterion (course-assistant's integrity sheet, its D-53/D-66): a declared label set and its mapping to judge outputs (e.g. V/C on a whole conversation from task_scores details), context shown only to the labeller (never in sheets or reports), the labelling page with a context pane and the label keys, opaque keys bound to the exact item, per-judge kappa with a bootstrap CI on the held-out half, refusal of stale or re-rubricked verdicts, a blind relabel round; course-assistant's cases are the tests [D-69]
