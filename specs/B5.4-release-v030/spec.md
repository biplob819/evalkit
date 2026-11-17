# B5.4 — v0.3.0 and downstream migration guide

**Wave 5. Depends on:** B5.2, B5.3, B5.6, B5.12, B5.13. **Decisions:** D-61 to D-72. **Owner step:** the tag and the PyPI publish are approved by the owner in the EvalKit session, never by a relayed message.

## Goal
Ship v0.3.0 so the seven downstream projects can move to one public, tested EvalKit, each with an exact list of what to change.

## Deliverables
- **V03-01:** version 0.3.0 in `pyproject.toml`, `evalkit.__version__` and `uv.lock`; a consolidated 0.3.0 section in `changelog.md` (public additions, behaviour changes, the 0.2 → 0.3 notes from docs/versioning.md); the downstream canary (`scripts/downstream_canary.py --all` on fresh clones) run against the release candidate and recorded in `docs/measurements/downstream-canary-<date>-rc.json`, every project `pass`, `known-drift` (explained) or `no-tests`; full CI green on all three operating systems at the release-candidate commit.
- **V03-02:** `docs/downstream.md`: per registered project, its current pin, the 0.3.0 requirement line to use, every EvalKit import it uses today that moved or became public (from `imports_not_public` in the canary record and `scripts/check_public_api.py` `PROMOTED`/`MOVED`) with the exact replacement, single-project internals that stay internal with the recommended replacement, behaviour changes that affect it (e.g. gemma: add `systems` to its config spec; value_check three outcomes; one-sided gate bounds; summary.json `efficiency`), and the new capabilities it asked for with their public names (B5.3 gate, B5.6 compare, B5.12 citation support, B5.13 scorers).
- **V03-03:** after the owner approves in this session: tag `v0.3.0`, the release workflow publishes by trusted publishing, the post-release smoke passes on three OSes, `docs/measurements/release-v0.3.0-<date>.json` records commit, files and sha256 (checked like v0.1.0 by tests/test_published.py), and every project session is told the tag and commit.

## Tests
- Version consistency (pyproject, __version__, uv.lock, changelog heading).
- The migration guide names every non-public import in the latest canary record and every replacement resolves (imports succeed).
- The RC canary record covers every registered project with tests.

## Out of scope
v0.4.0 nodes (B5.7, B5.8, B5.9, B5.11, B5.14, B5.15 ship in v0.4.0 even where already green).
