# A2.4 run state

**Now:** green

## History
- 2026-11-14 — built; verifier pass 1 disproved JDG-02..05 (expired batches never resubmitted, partial cache re-paid, leaky custom ids, no budget hold, unbounded sleep, claim-only redaction, contradictory evidence, same-provider dual mode). Rewritten (D-35, L-24).
- 2026-11-14 — pass 2 disproved JDG-02, JDG-03, JDG-05 (prefix redaction corrupting words, cap overshoot on resume, leaked holds, double submit, unreported retry spend, cached transient failures). Fixed.
- 2026-11-14 — pass 3 disproved JDG-02, JDG-03, JDG-05 (Unicode and clinical redaction gaps, concurrent double-processing, resume livelock, retry errors blocking batches, double-counted cost). Fixed (D-38).
- 2026-11-14 — pass 4 confirmed JDG-01..08; should-fix items (collect timeout covering downloads, quote wrappers, cross-process resolution, cached provider errors, superscript folding) fixed with regressions.
- 2026-11-14 — live planted-claim check: both judges 10/10 planted unsupported, 5/5 controls supported ($0.077 per run, run twice).
- 2026-11-14 — converge passed twice; rows flipped green.
