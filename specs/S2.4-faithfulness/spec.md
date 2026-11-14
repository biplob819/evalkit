# S2.4 — Explanation faithfulness

**Wave 2. Depends on:** A2.3, A2.4. **Unblocks:** downstream projects (US6).

## Goal
A downstream project (an agent explaining its answer, a RAG system summarising documents) can ask: which statements in this explanation are supported by the evidence it was given? The scorer reuses the pinned claim extractor and the blind judges, so faithfulness is measured exactly like groundedness.

## Outputs (`evalkit/scorers/faithfulness.py`)
- `FaithfulnessItem(id, question, explanation, evidence: tuple[str, ...])` (FTH-01).
- `async faithfulness(items, *, executor, config, adapters, prices, judges=None, live=True) -> list[FaithfulnessResult]`: extracts claims from each explanation with the A2.3 extractor (every evidence sentence counts as cited, since explanations rarely cite), judges each claim against the evidence with the A2.4 judges (live by default; batch on request), and returns per item: `score` (share of judged claims supported), `fully_faithful`, the claims with each judge's verdict and evidence, and error counts (FTH-01, FTH-02).
- Caching and spend follow the executor: re-scoring the same items is free.
- Fixtures and criteria (FTH-03): a small set of explanations with known faithful and unfaithful claims, scripted model replies, and a live check script mirroring the planted-claim check.

## Rules
- Unfaithful means not supported by the given evidence, even if true in general (B6 applies).
- An explanation with no extractable claims scores 0 and is not fully faithful (nothing was verified).
