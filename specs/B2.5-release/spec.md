# B2.5 — Open-source packaging and v0.1.0

**Wave 3. Depends on:** B2.3, B2.4. **Unblocks:** nothing (release gate).

## Goal
Anyone can `pip install evalkit-harness`, get a first result in under five minutes, extend it with their own task, scorer or adapter, and trust what the package does with their keys and data. v0.1.0 ships from a tag through trusted publishing, with a contract test against the published package.

## Outputs
- HAR-08 / OSS-01: the `evalkit-harness` name on PyPI with a trusted publisher for `biplob819/evalkit` (`release.yml`, environment `pypi`); version 0.1.0 in `pyproject.toml`; `evalkit` CLI and import.
- OSS-02: CI matrix (3.11–3.13 × Linux/macOS/Windows, already in `ci.yml`) plus a post-release job that runs `uvx --from evalkit-harness==<tag> evalkit doctor --offline` and `evalkit report --example`.
- OSS-03: `docs/quickstart.md` (first result in under 5 minutes: install, init, doctor, report --example, dry run, run), `docs/writing-a-task.md`, `docs/plugins.md` (scorers and adapters via entry points), `docs/calibrate-your-task.md` (exists), `docs/cost.md` (estimates, caps, batch vs live, resume).
- OSS-04: a second public grounded-QA task (licence-checked) and the user-data template (`evalkit init` already writes a JSONL template; add a CSV import path or document conversion).
- OSS-05: CONTRIBUTING.md, SECURITY.md (key handling, reporting a vulnerability), CODE_OF_CONDUCT.md, issue and PR templates.
- OSS-06: `examples/github-action/evalkit.yml`: runs a task and fails the job when a threshold fails (exit 1), surfaces caveats and the report as an artifact.
- OSS-07: PRIVACY section/file: no telemetry; data goes only to the providers configured; what is stored locally.
- OSS-08: semver policy (results format and `evalkit.scorers` public API), an import contract test listing every public symbol, and the v0.1.0 tag.

## Owner steps
- Create or log in to a pypi.org account (2FA), add the pending trusted publisher for `evalkit-harness`.
- Approve the v0.1.0 tag after reviewing README and docs.
