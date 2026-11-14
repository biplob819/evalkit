# B2.3 — README chart and dashboard deploy

**Wave 3. Depends on:** B2.1. **Unblocks:** B2.5.

## Goal
A visitor understands the headline result in under a minute (US5): the README opens with one chart drawn from a real run's `summary.json`, three sentences, and a three-line install-and-run snippet; the full report is one click away on a public page.

## Outputs
- `evalkit.report.readme_chart(summary) -> str`: a standalone SVG (light and dark readable, no external fonts) of the headline per model and judge with CIs, carrying the judge-mode and calibration stamps in the chart itself so the image cannot be shared without its caveats (REL-01). `scripts/readme_chart.py RUN_DIR` writes `docs/assets/headline.svg`.
- README opening (REL-02): the chart, three sentences (what was measured, the result with intervals, the caveats), `pip install evalkit-harness` / `evalkit run ...` snippet, PubMedQA credit and licence line, link to the public report.
- Public report (REL-03): the run's `report.html` published as a static page (host per owner decision), with the deploy reproducible from a script or workflow.

## Rules
- The chart and sentences are generated from `summary.json`, never typed by hand; a test regenerates them and compares.
- Until a calibrated dual-judge run exists, the README shows the stamps prominently and says the numbers are provisional.
