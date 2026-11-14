# B2.4 — Public CLI and onboarding

**Wave 3. Depends on:** B2.2. **Unblocks:** B2.5.

## Goal
A stranger gets from `pip install evalkit-harness` to a first result without reading the code: `init` scaffolds a project, `doctor` says what is missing, `report --example` shows what a result looks like with no keys, and every run prints its caveats.

## Commands
- `evalkit init [DIR]` (ONB-01): writes `evalkit.yaml` (four models, two cross-family judges, a cost cap), `.env.example` (key names only), `tasks/my_task.yaml` and `tasks/my_task.jsonl` (a three-case template for the user's own data), and a `.gitignore` entry for `.env`, `runs/` and `.evalkit/`. Never overwrites an existing file; `--force` replaces only EvalKit's own files.
- `evalkit doctor [--offline] [--json]` (ONB-02): Python version, installed provider SDKs, which keys are set (names only, never values), whether the providers' API hosts resolve and accept a TCP connection (skipped with `--offline`), the age of `prices.yaml` (warn after 90 days), and the cache folder size. Exit 0 when ready, 2 when something blocks a run.
- `evalkit report --example [--out DIR]` (ONB-03): renders the bundled example run (the golden fixture run, shipped in the package) with no keys and no network.
- ONB-04..07 are delivered by B2.2 (`--dry-run`, `--max-cost` with exit 3, exit codes and `--json`, `.env` loading and named missing keys) and re-verified here.
- Caveat banners (ONB-08): every `evalkit run` and `evalkit report` output prints the `stamp_lines` caveats (single judge, uncalibrated, judging pending) in text and `--json` (`caveats`).
- Safety rules (L-30): `report --example` refuses a non-empty `--out` that does not hold the example; `init --force` never rewrites `tasks/*`; `doctor` reads the config and blocks on a missing key or SDK for any provider it needs; `run` refuses before spending when an SDK is missing; the README install line includes the provider extras.
