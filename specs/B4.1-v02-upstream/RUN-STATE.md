# B4.1 run state

**Now:** building. Converge passed twice in a row (2026-11-15). Proof cells written, rows stay `building`; next: skeptical verifier, then green and commit (owner approval), then the owner tags v0.2.0.

## History
- 2026-11-15 — node added for the downstream course-assistant need (its OWNER D-11, node X1.1); D-59.
- 2026-11-15 — node_start GO (baseline ca22b5c); tests first in tests/test_v02.py (27 test functions, 34 cases), then build: `SystemAdapter` + `generate_system` node, `Case.meta`, task scorers (`evalkit.pipeline.task_scorers`), slices in aggregate and report, omit-when-empty serialisation for every new field, version 0.2.0.
- 2026-11-15 — v0.1 identities pinned in tests from git ca22b5c before any change (run id, config hash, Generate cache key, task hash, fixture dumps); all unchanged. Golden summary and report byte-identical without regeneration.
- 2026-11-16 — verifier pass 1 (fresh context): SUT-01, SUT-02, TSC-01, SLC-01, BWC-01 all CONFIRMED; v0.1-shaped runs byte-identical against baseline ca22b5c; 1179 tests and all 12 gates pass. Compatibility gap fixed in the docs: changelog and docs/versioning.md now list all three breaking changes (unknown scorer exits 2; a v0.1 task naming a registered plugin now runs it; plugins need keyword context). GREEN. Deferred should-fix: slice values collide ('(none)' literal with missing, True with 'true'); a huge scorer value exits 2 with an OverflowError that names no scorer; a system alias still warns 'no price ... costing it at $0'.
