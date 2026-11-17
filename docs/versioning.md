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
  `RawCompletion`, `BatchRequest`, `BatchResult`, `register`, `discover`, `get_adapter`), and
  since 0.2.0 systems under test (`SystemAdapter`, `register_system`, `discover_systems`,
  `get_system`).
- **`evalkit.schemas`**: the results format (`RunSummary`, `ModelSummary`, `Record` and the
  other names in its `__all__`), versioned by `SCHEMA_VERSION`.
- **The plugin entry-point groups** `evalkit.scorers`, `evalkit.adapters` and (since 0.2.0)
  `evalkit.systems`.
- **The task YAML and dataset formats** described in [writing-a-task.md](writing-a-task.md).

Everything else, including `evalkit.pipeline`, `evalkit.runtime` and `evalkit.demo`, is
internal and may change in any release.

## Results format

`summary.json` and the per-case records carry `schema_version`. A field is never renamed or
removed within a major version of the format; new optional fields may appear in a minor
release. A reader should ignore fields it does not know.

0.2.0 added optional fields (`Case.meta`, `Record.task_scores`, `ModelSummary.task_scores`,
`RunSummary.slices`, `RunConfig.systems`, `TaskConfig.slices`). Each is left out of the JSON
while it is empty, so a run that uses none of them writes exactly what 0.1.0 wrote and 0.1.0
can read it; a run that uses them needs 0.2.0 or later to read its records and summary.

## 0.3.0 additions

- `evalkit.schemas.CitationVerdict` and the optional fields `Record.citation_support`, `ModelSummary.citation_precision` and `ModelSummary.aim_citation_rate` (B5.12, D-65), each omitted from JSON when empty.

## Deprecation

A public name that is going away keeps working, with a `DeprecationWarning`, for at least one
minor release before it is removed, and the changelog names its replacement.

## Scorer and cache versions

Each scorer and pipeline node has a version. When its output changes, its version changes, so
cached results from the old version are never mixed with new ones.


## 0.1 → 0.2 breaking changes

v0.1 runs whose tasks name only the six benchmark scorers are byte-identical under 0.2. Three things change for other tasks:

1. An unknown scorer name exits 2 (v0.1 ignored it).
2. A scorer name that matches a registered plugin now runs inside `evalkit run` (v0.1 ignored it), so records and the summary gain `task_scores` and the report gains a column. Files with these fields cannot be read by 0.1.0.
3. A task scorer is called as `f(case, completion, model=, claims=, verdicts=, scores=)`. A v0.1-style plugin `def f(case, completion)` must add `**ctx`, or the run exits 2.
