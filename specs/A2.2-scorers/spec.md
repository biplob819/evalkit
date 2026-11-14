# A2.2 — Deterministic scorers

**Wave 2. Depends on:** N1.1, N1.4. **Unblocks:** A2.5, S2.1–S2.3, D3.1.

## Goal
The scores that need no model: label correctness, citation validity, cost and latency, behind one `Scorer` protocol that downstream repos import from `evalkit.scorers` and extend through entry points.

## Outputs
- `evalkit/scorers/base.py` — `Scorer` protocol (`name`, `version`, `__call__(case, completion, **ctx) -> ScoreResult`), `ScoreResult(value, details)`, registry, `get_scorer`, entry-point discovery under `evalkit.scorers` with per-plugin failure isolation (DET-04, DET-05).
- `evalkit/scorers/deterministic.py` — `label_correct` (DET-01), `citation_valid` (DET-02), `cost`, `latency` (DET-03).

## Rules
- A completion with an error or no answer scores 0 on label and citations (errors count as wrong, NFR5); cost and latency still report what was spent.
- Label match is exact after trimming and case-folding; the reference label comes from the case.
- Citation validity = distinct cited sentence numbers inside 1..len(context) ÷ distinct cited numbers; no citations scores 0 (an answer that cites nothing is not checkable).
- Scorers are pure functions of (case, completion): same inputs, same result.
