# B5.1 — Downstream registry and canary

**Wave 5. Depends on:** B4.1. **Unblocks:** B5.2 (the audit decides what becomes public). Decision: D-61.

## Goal
Know, before any EvalKit release, which downstream projects use which EvalKit names and whether their EvalKit-facing tests still pass against the code about to ship.

## Why
Seven public projects build on EvalKit (D-61, D-62). On 2026-11-17 their pins differ and five import internal modules, one of them private names. A refactor inside `evalkit.pipeline` or `evalkit.cost` can break a project silently, and nothing today says which internals are really load-bearing.

## Interface

### Registry `docs/downstream.tsv` (DSC-01)
Columns: `project`, `repo` (`owner/name` on GitHub), `branch`, `pin` (the EvalKit requirement as written in the project's `pyproject.toml`), `install` (the `uv sync` arguments: extras and groups), `tests` (`auto`: every test file that imports `evalkit`, or `none`), `summary` (path of the committed `summary.json`, or `-`), `deselect` (comma-separated pytest node ids of tests that check the installed pin itself and so fail by design when the canary replaces it, or `-`), `note`.
- One row per project (seven); `respectful-offers` is listed with `tests=none` (it copies the design and imports nothing).
- `tests/downstream/test_registry.py` checks the shape offline; with `EVALKIT_CHECK_DOWNSTREAM=1` it also fetches each repo's `pyproject.toml` at the branch and checks that the recorded pin matches.

### Import audit `scripts/downstream_imports.py` (DSC-02)
`python scripts/downstream_imports.py <checkout> [--json] [--tests]`
- Parses every `.py` file under `src/`, `tests/`, `scripts/` and `demo/` with `ast` (never imports it). Skips virtual environments and `.git`.
- For each `from evalkit... import name` and `import evalkit...`: module, name, file count, and a class:
  - `public`: the module and name are in the public contract (`scripts/check_public_api.py` `PUBLIC`);
  - `public-elsewhere`: the name is public but imported from a submodule path (e.g. `evalkit.adapters.base.SystemAdapter`), so the fix is the import path;
  - `internal`: anything else; `private` when the name starts with `_`.
- `--tests` prints only the test files that import EvalKit (what the canary runs).
- Exit 0 always for a readable checkout; 2 for an unreadable path. `--self-test` proves each class is assigned and that a syntax error in one file is reported, not fatal.

### Canary `.github/workflows/downstream.yml` (DSC-03)
- Triggers: weekly schedule, `workflow_dispatch`, and tags `v*` (before the release job's publish is approved).
- One job per registry row with tests `auto` (matrix from the registry): check out EvalKit and the project (`actions/checkout` with `repository:`), `uv sync --frozen <install>` in the project, then `uv pip install --no-deps -e <evalkit>` so this checkout replaces the pin, then `uv run --no-sync pytest <files from --tests> -m "not live"` with each `deselect` id passed as `--deselect`.
- No secrets and no API keys in the environment; `permissions: contents: read`; each job uploads the audit JSON and the pytest summary as an artifact.
- A failing project fails its own job only (`fail-fast: false`).
- `scripts/downstream_canary.py` is the same run locally against checkouts already on disk (used by the golden test and by the owner before tagging).

## Out of scope
- Changing any downstream repo (D-61 rule 4).
- Promoting names (B5.2) and gates (B5.3).
