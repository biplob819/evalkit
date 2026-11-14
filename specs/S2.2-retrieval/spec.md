# S2.2 — Retrieval metrics

**Wave 2. Depends on:** A2.2. **Unblocks:** downstream retrieval projects.

## Goal
Standard ranking metrics for a retrieved list against a set of relevant ids, as plain functions exported from `evalkit.scorers`.

## Interface
- `recall_at_k(retrieved, relevant, k) -> ScoreResult` — share of relevant ids in the top k.
- `mrr(retrieved, relevant) -> ScoreResult` — 1 / rank of the first relevant id, 0 if none.
- `ndcg_at_k(retrieved, relevant, k, gains=None) -> ScoreResult` — binary gains by default, graded when `gains` maps id → gain; log2 discount; normalised by the ideal ordering.

## Rules
- `retrieved` is an ordered sequence of ids as the system returned them. `k` counts returned positions; a repeated id earns nothing after its first occurrence, so duplicates waste slots instead of earning double credit.
- `relevant` empty: recall and nDCG are undefined and raise `ValueError`; MRR is 0.0.
- `k` must be ≥ 1; values are in [0, 1].
