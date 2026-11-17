# B5.14 run state

**Now:** GREEN (2026-11-17).

## History
- 2026-11-17 — lane opened (D-66).
- 2026-11-17 — spec completed (public module `evalkit.heldout`, `python -m evalkit.heldout`); tests first in `tests/test_heldout_discipline.py` with temporary git repositories (tampered middle line, rewritten and restored history, force-push rewrite, merge that drops a parent's line, append after a merge, score twice, leak into a tracked file); built `src/evalkit/heldout.py`; docs in `docs/heldout.md`.
- 2026-11-17 — dogfood on EvalKit's private held-out agent tasks (paths, ids and counts only): the default scan flagged 7 of 8 tasks in 28 public files, all shared canaries, SQL joins and one instruction template (words 5 to 17 of one goal, shared with five public tasks and the build script), no whole goal. Added `fields` and `public` (public word windows are excused, whole held-out strings never); `--field goal --public src/evalkit/agents/tasks` scans 444 tracked files clean. `heldout/definitions.yaml` is a build input, not a case file, and is refused as one.
- 2026-11-17 — merged; verifier pass 1: HLD-01..04 confirmed, 2 must-fix (score-once not enforced on the log itself; case-insensitive path fail-open) and 6 should-fix, all fixed in a fix lane. Windows CI: a test's rmtree hit read-only git objects; fixed. Verifier pass 2: all confirmed, no must-fix; should-fixes applied (a `--field` no case has is an error, deep nesting and unreadable folders exit 2). GREEN.
