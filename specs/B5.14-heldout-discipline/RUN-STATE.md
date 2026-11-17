# B5.14 run state

**Now:** built in its lane (D-66); waiting for converge and the verifier.

## History
- 2026-11-17 — lane opened (D-66).
- 2026-11-17 — spec completed (public module `evalkit.heldout`, `python -m evalkit.heldout`); tests first in `tests/test_heldout_discipline.py` with temporary git repositories (tampered middle line, rewritten and restored history, force-push rewrite, merge that drops a parent's line, append after a merge, score twice, leak into a tracked file); built `src/evalkit/heldout.py`; docs in `docs/heldout.md`.
- 2026-11-17 — dogfood on EvalKit's private held-out agent tasks (paths, ids and counts only): the default scan flagged 7 of 8 tasks in 28 public files, all shared canaries, SQL joins and one instruction template (words 5 to 17 of one goal, shared with five public tasks and the build script), no whole goal. Added `fields` and `public` (public word windows are excused, whole held-out strings never); `--field goal --public src/evalkit/agents/tasks` scans 444 tracked files clean. `heldout/definitions.yaml` is a build input, not a case file, and is refused as one.
