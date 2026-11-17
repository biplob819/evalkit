# B5.8 run state

**Now:** GREEN (2026-11-17).

## History
- 2026-11-17 — lane opened; set adopted from gemma-grounded-health (D-68).
- 2026-11-17 — set vendored with provenance; spec completed; tests written first; `evalkit.paraphrase` built (loader, built-in tasks, builder, report, `evalkit report --paraphrase`); gates clean.
- 2026-11-17 — merged; verifier: PAR-01 and PAR-02 CONFIRMED (vendored bytes equal gemma c7d70d2; seed-7 draw 55/34/11 on the real PQA-L; paired bootstrap matches). Must-fix (errored answers could flag contamination) and six should-fixes applied in a fix lane: errors excluded and counted, coverage per model, settings compared (a flag with different settings is "confounded"), the builder aligned with gemma's call settings, the estimate a strict upper bound, `--publish` refuses before writing, exit 3 tested. GREEN; owner spot-check still pending before any result is published.
