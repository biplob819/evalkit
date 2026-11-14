# Versioning policy

EvalKit follows [Semantic Versioning](https://semver.org/). Before 1.0.0, a minor release
(0.x → 0.y) may break the public API, and the changelog says how to migrate; patch releases
(0.x.y) never do.

## What is public

These are covered by the policy, and `tests/test_public_api.py` fails if one disappears:

- **The command line**: `evalkit` subcommands, their options and the exit codes.
- **`evalkit.scorers`**: every name in its `__all__` (the scorer functions, `scorer`,
  `Scorer`, `ScoreResult`, `discover`, `get_scorer`).
- **`evalkit.adapters`**: the adapter interface (`ModelAdapter`, `CompletionRequest`,
  `RawCompletion`, `BatchRequest`, `BatchResult`, `register`, `discover`, `get_adapter`).
- **`evalkit.schemas`**: the results format (`RunSummary`, `ModelSummary`, `Record` and the
  other names in its `__all__`), versioned by `SCHEMA_VERSION`.
- **The plugin entry-point groups** `evalkit.scorers` and `evalkit.adapters`.
- **The task YAML and dataset formats** described in [writing-a-task.md](writing-a-task.md).

Everything else, including `evalkit.pipeline`, `evalkit.runtime` and `evalkit.demo`, is
internal and may change in any release.

## Results format

`summary.json` and the per-case records carry `schema_version`. A field is never renamed or
removed within a major version of the format; new optional fields may appear in a minor
release. A reader should ignore fields it does not know.

## Deprecation

A public name that is going away keeps working, with a `DeprecationWarning`, for at least one
minor release before it is removed, and the changelog names its replacement.

## Scorer and cache versions

Each scorer and pipeline node has a version. When its output changes, its version changes, so
cached results from the old version are never mixed with new ones.
