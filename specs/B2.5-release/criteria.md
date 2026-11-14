# B2.5 acceptance criteria

**HAR-08** The `evalkit-harness` project exists on PyPI owned by the owner, with a trusted publisher for this repository's release workflow.
**OSS-01** Tagging `v0.1.0` builds and publishes through trusted publishing (no stored token); the tag/version check refuses a mismatch; CLI `evalkit` and import `evalkit` work from the installed package.
**OSS-02** CI passes on Python 3.11–3.13 on Linux, macOS and Windows; a post-release job installs the published version with uvx and runs `doctor --offline` and `report --example`.
**OSS-03** The five docs exist, every command they show runs (doc commands are extracted and exercised by a test with scripted adapters), and the quickstart reaches a report in under five minutes of wall time with no keys via `--example`.
**OSS-04** A second public grounded-QA task (licence recorded in DATA_CARD) and a user-data template run end to end on scripted adapters.
**OSS-05** CONTRIBUTING, SECURITY, CODE_OF_CONDUCT and issue/PR templates exist; SECURITY states key handling and a private reporting route.
**OSS-06** The GitHub Action example runs a task and fails the job on exit code 1, passes on 0, and uploads the report.
**OSS-07** The privacy statement is accurate against the code: no network calls except configured providers (and `doctor`'s reachability check, and the PubMedQA download), no telemetry.
**OSS-08** A semver policy is documented; a contract test imports every public symbol of `evalkit`, `evalkit.scorers` and the results schemas; v0.1.0 is tagged.
