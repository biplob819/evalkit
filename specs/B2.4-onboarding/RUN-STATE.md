# B2.4 run state

**Now:** green

## History
- 2026-11-14 — built: init, doctor, report --example (bundled example run), caveats in run/report output; shared outputs now 0644.
- 2026-11-14 — verifier confirmed the wheel install flow, ONB-01/03/04-08; partly disproved ONB-02 (doctor ready without SDKs or a needed key) and found two data-loss paths (example over a run folder, init --force over task data); all fixed with regressions (L-30).
- 2026-11-14 — converge passed twice; rows flipped green.
