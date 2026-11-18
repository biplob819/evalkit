# B5.10 — v0.4.0

**Wave 5. Depends on:** B5.7, B5.8, B5.9, B5.11, B5.14, B5.15, B5.16. **Decisions:** D-62, D-64, D-65, D-73 to D-77. **Owner step:** the tag and the PyPI publish are approved by the owner in the EvalKit session, never by a relayed message.

## Goal
Ship v0.4.0, which formally adds the ledger and drift monitor, the paraphrase probe, the calibration integrity sheet, the security kit, held-out discipline, the `sql_match` equivalences and the strict `value_check`. The modules marked provisional in 0.3.0 become covered by the usual deprecation window.

## Deliverables
- **V04-01:** version 0.4.0 in `pyproject.toml`, `evalkit.__version__` and `uv.lock`; a consolidated "## 0.4.0" changelog section covering every addition and behaviour change since 0.3.0. These include: clustered clarification intervals; strict run-folder detection; the D-76 claims-per-item estimate; `--out` accepting a folder; and the canary cloning with full history. `value_check` and its companions are public again, with the D-74 `WITHHELD` guard removed. The docs/versioning.md 0.3.0 "provisional" note is resolved by a 0.4.0 section. docs/downstream.md gains a 0.3 → 0.4 section per project. The RC canary runs on fresh clones of every registered project and is recorded as docs/measurements/downstream-canary-<date>-rc4.json; every project is pass, known-drift (explained) or no-tests. CI is green on Linux, macOS and Windows at the RC commit, and a clean-venv wheel install works on Python 3.11 and 3.12.
- **V04-02:** after the owner approves in this session: tag `v0.4.0`; the release workflow publishes by trusted publishing; the post-release install check passes on three OSes; docs/measurements/release-v0.4.0-<date>.json records the commit, files and sha256; and every project session is told the tag and commit.

## Tests
- Version consistency.
- `value_check` names import from `evalkit.scorers` and are in PUBLIC.
- The guide's 0.4 sections name every changed import, and each replacement resolves.
- The RC canary record covers every registered project.
- The release record matches the tag, the tree and the PyPI files.

## Inputs noted while waiting
- The 2026-11-18 canary (docs/measurements/downstream-canary-2026-11-18.json) records fraud-review-bedrock importing 18 internal EvalKit names for its local strict valuecheck and runner, including `evalkit.run.make_adapters`, `evalkit.runtime.Executor`, `evalkit.adapters.base.CompletionRequest`, `evalkit.env.KEYS` and `evalkit.cost.estimate_run_cost`. The 0.3 → 0.4 guide must classify each one under D-61 rule 2 (public if two or more projects use it, otherwise internal with a replacement).
