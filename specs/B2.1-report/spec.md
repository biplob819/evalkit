# B2.1 — Report and dashboard

**Wave 3. Depends on:** A2.5, A2.6. **Unblocks:** B2.2, B2.3.

## Goal
`render_report(summary, records, cases)` turns one run into a single self-contained HTML file (D-42) that a PM can read in a minute and an engineer can audit claim by claim, plus `charts.vl.json` for interactive exploration. It works offline, prints, and never hides the run's caveats.

## Outputs (`evalkit/report/`)
- `render_report(...) -> str` and `write_report(dir)`: `report.html` (inline SVG and CSS, a little vanilla JS, no network requests) and `charts.vl.json` (Vega-Lite specs for the same data) (DSH-01).
- Quality-versus-cost scatter: headline per judge (y) against cost per 100 cases (x), with 95% CI whiskers, one mark per model and judge, the threshold line when one is set, and a data table beside it (DSH-02).
- Model table: headline per judge, accuracy, groundedness, citation validity, coverage, cost per correct answer, latency p50/p95, errors by type, and a PASS/FAIL badge with the failed thresholds spelled out. Badges use text and shape, never colour alone. The pairwise table marks "separated" or "tie" (Holm, D-36), and labels the difference intervals as unadjusted (DSH-03).
- Failure explorer: answers that are not correct-and-grounded, filterable by model and judge, each showing the question, answer, claims, both judges' verdicts with evidence and quotes, and the cited sentences. Keyboard reachable (DSH-04).
- Calibration panel: kappa per judge and per family with CIs, the confusion matrix, extraction error rate and self-consistency when calibrated; otherwise the "not calibrated" stamp with a link to the guide. Self-preference table in dual mode (DSH-05).
- Every page carries the research-only banner (B7), the `stamp_lines` caveats at the top, a "judging pending" state that shows deterministic metrics only, visible focus, sufficient contrast in light and dark themes, and charts with `<title>`/`<desc>` (DSH-06).

## Rules
- All text from answers, claims and sentences is HTML-escaped; the report never executes model output.
- The report is a pure function of its inputs: the same inputs give byte-identical HTML.
