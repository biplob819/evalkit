# B5.8 — Paraphrase contamination probe

**Wave 5. Depends on:** N1.4, A2.1. Decisions: D-62, D-66, D-68. Method: DATA_CARD.md (DAT-06).

Skeleton written by the lead (D-66); the lane builder completes Goal, Why, Interface and Out of scope before tests.

## Capabilities
- **PAR-01**: Paraphrase set built by the PRD's documented method (P4): deterministic, cached, with its prompt and model recorded and cost estimated before any call [D-62]
- **PAR-02**: Contamination report: original-vs-paraphrase accuracy per model with a paired CI [D-62]

## Set (D-68)
Adopt gemma-grounded-health's set (commit c7d70d2) with provenance; owner spot-check of 20 kept and all rejected before publishing results.
