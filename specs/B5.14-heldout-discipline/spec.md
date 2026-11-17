# B5.14 — Held-out discipline and tamper-evident logs

**Wave 5. Depends on:** A2.6, C3.1. Decisions: D-61 to D-66.

Skeleton written by the lead so the lane can start (D-66); the lane's builder completes Goal, Why, Interface and Out of scope before tests. Inputs collected before the spec are in `inputs.md` when present.

## Capabilities
- **HLD-01**: Held-out split pinned by a committed manifest of hashes and counts; reports carry split-level aggregates only [D-65]
- **HLD-02**: Score-once guard: each held-out case is scored once per rubric version [D-65]
- **HLD-03**: Exposure scan: the build fails if held-out text appears in tracked files [D-65]
- **HLD-04**: Tamper-evident log: hash-chained append-only JSONL checked in CI against the push base, every merge parent and the default branch tip; a calibration is certified only with a matching logged line (case ids and rubric hash) (course-assistant, its D-55/D-59) [D-65]
