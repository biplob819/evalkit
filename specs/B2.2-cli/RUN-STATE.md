# B2.2 run state

**Now:** green

## History
- 2026-11-14 — built: CLI (run, calibrate, report), resumable run folders, live judging sharing the batch cache, local JSONL tasks; CLI tests found and fixed an A2.5 pending-aggregation crash.
- 2026-11-14 — verifier confirmed CLI-02/03/05 and resume-without-double-pay across crash, cap, pending and model additions; disproved CLI-01 and CLI-04 (unexpected errors exited 1 with tracebacks, `--max-cost 0`, per-invocation cap, live re-pay of open batches, dry run needing keys, silent judge downgrade, `--force` stranding labels); all fixed with regressions (D-45, L-29).
- 2026-11-14 — converge passed twice; rows flipped green.
