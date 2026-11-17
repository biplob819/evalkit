# B5.12 run state

**Now:** GREEN (2026-11-17).

## History
- 2026-11-17 — node added for gemma-grounded-health (its OWNER D-44); D-65.
- 2026-11-17 — built in its own lane (D-66): a generic judging rubric whose default reproduces the claim judge byte for byte (JUDGE_PROMPT_HASH 9a3a8246049f6f85, cache keys, custom ids, run outputs pinned from before the change), a citation rubric (CITATION_PROMPT_HASH be34ebe577bc96ae), records, metrics with bootstrap CIs, slices, report, docs. Merged to main (d51d7bc); converge needed one fix: the estimate fingerprint differs on Python 3.11 only because 3.12 made float sum() compensated (L-27), so it is pinned per version.
- 2026-11-17 — verifier: CIT-01, CIT-02, CIT-03 CONFIRMED (claim judge independently re-derived from the old tree; byte-identical outputs). Should-fixes applied: CIT-03 replies relabelled as hand-written (no real judge produced them; a real-judge check is an owner-approved follow-up); empty per-judge citation lists omitted; `calibrate score` keeps an unfinished run pending; unjudged citation claims shown in progress. Converge passed twice. GREEN.
