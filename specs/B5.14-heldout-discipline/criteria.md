# B5.14 acceptance criteria

**HLD-01** Held-out split pinned by a committed manifest of hashes and counts; reports carry split-level aggregates only [D-65]
**HLD-02** Score-once guard: each held-out case is scored once per rubric version [D-65]
**HLD-03** Exposure scan: the build fails if held-out text appears in tracked files [D-65]
**HLD-04** Tamper-evident log: hash-chained append-only JSONL checked in CI against the push base, every merge parent and the default branch tip; a calibration is certified only with a matching logged line (case ids and rubric hash) (course-assistant, its D-55/D-59) [D-65]
