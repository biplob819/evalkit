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

Since 0.3.0, the names two or more downstream projects import (D-61 rule 2), each module
listing exactly these in its `__all__` (its other names stay internal):

- **`evalkit.cost`** (since 0.3.0): `Price`, `load_prices`, `call_cost`, `estimate_tokens`.
- **`evalkit.retry`** (since 0.3.0): `with_retries`.
- **`evalkit.stats`** (since 0.3.0): `Resamples`, `interval`, `paired`, `holm`,
  `percentile_ms`; and (B5.3) `clopper_pearson`, `rate_wording`.
- **`evalkit.metrics`** and **`evalkit.gates`** (since 0.3.0, B5.3): every name in their
  `__all__` (the `metrics-1` format and `evalkit gate`; see [gates.md](gates.md)).
- **`evalkit.scorers.faithfulness`** (since 0.3.0): `faithfulness`, `FaithfulnessItem`,
  `FaithfulnessResult`, `JudgedClaim`. Import them from this module; they are not
  re-exported from `evalkit.scorers`.
- **`evalkit.calibration`** (since 0.3.0): `CalibrationPair` (`cluster`, `human`, `judge`),
  `cluster_kappa`, `kappa_from`, `agreement_table` and the `AgreementTable` it returns
  (tp/fn/fp/tn, agreement, strictness, leniency, kappa, `owner_constant`), at any label unit.
- **`evalkit.errors`** (since 0.3.0): `SchemaError`, raised when a model's output is not valid
  structured output.
- **`evalkit.identity`** (since 0.3.0): `JUDGE_PROMPT_HASH` and `EXTRACTOR_VERSION`, the
  values a current `Record` carries as `judge_prompt_hash` and `extractor_version`.

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

- `evalkit.scorers`: `answer_from_rows`, `unsupported_numbers`, `clarification_rates`, `ClarificationRates`, `Rule`, `RuleSet`, `RuleError`, `rule_compliance`, `rule_compliance_scorer`, `render_evidence`, `ValuePhrase`, `value_check`, `value_check_scorer` (B5.13, D-65).
- `evalkit.schemas.CitationVerdict` and the optional fields `Record.citation_support`, `ModelSummary.citation_precision` and `ModelSummary.aim_citation_rate`, and the same two on `SliceSummary` (B5.12, D-65), each omitted from JSON when empty.

- The `evalkit gate` command (B5.3, D-62..D-65), with a summary file and the options `--config`, `--json` and `--out`: gates on any named metric from a `metrics-1` file or an EvalKit `summary.json`; exit codes 0/1/2/3/4 with precedence 2 > 3 > 4 > 1 > 0; no model or network call. See [gates.md](gates.md).
- `evalkit.metrics` (B5.3): the frozen `metrics-1` format (`MetricsSummary`, `Metric`, `Provenance`, `parse_metrics`, `load_metrics`, `MetricsError`, `SCHEMA`), the standard efficiency names (`EFFICIENCY_NAMES`, D-67), `from_run_summary` and `efficiency_metrics`, and `safe_text`.
- `evalkit.gates` (B5.3): `Gate`, `GatesFile`, `parse_gates`, `load_gates`, `evaluate`, `run_gate`, `GateReport`, `GateRow`, `RunStatus`, `disparity_gap`, `render_text`, the input-classes table `INPUTS` and the exit-code constants.
- `evalkit.stats.clopper_pearson` and `evalkit.stats.rate_wording` (B5.3, GAT-07): exact binomial intervals and the "0 of n, below X% at 95%" wording.
- The run report gains an Efficiency section (cost per case and per correct, latency p50/p95/p99 with seeded 95% bootstrap intervals, tokens per case, error rate); `summary.json` is unchanged. `run.json` gains `stopped_at_cost_cap`, written only when a run stops at its cap (omitted otherwise, so run folders keep their bytes).

## 0.4.0 additions (on main ahead of the release)

- `evalkit.heldout` (B5.14, D-65): held-out manifest of hashes and counts, score-once guard, exposure scan and hash-chained append-only log; `python -m evalkit.heldout manifest|verify|scan|verify-log` (exit codes 0, 1, 2). See [heldout.md](heldout.md).
- `evalkit.paraphrase` (B5.8, D-68): the paraphrase contamination probe set, its builder and the original-vs-paraphrase report; built-in tasks `pubmedqa-paraphrase` and `pubmedqa-paraphrase-original`; `evalkit report --paraphrase ORIGINAL_RUN PARAPHRASE_RUN`; `python -m evalkit.paraphrase verify|build` (exit codes 0, 2, 3). See [contamination.md](contamination.md).
- `evalkit.integrity` (B5.9, D-69): calibration sheets for any declared two-way criterion (opaque keys per export, labeller-only context pane, per-judge held-out kappa with a bootstrap CI) and labeller consistency (blind repeats and planted items in a second pass), certified only through a `calibration_scored` line in the B5.14 log; the `evalkit calibrate` subcommands `export`, `label`, `import` and `score` take `--criterion FILE`, with `--manifest`, `--planted`, `--context`, `--relabel`, `--log` and `--rubric-hash`. See [calibrate-your-task.md](calibrate-your-task.md#calibrate-any-criterion).

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

## 0.2 → 0.3 migration

Nothing breaks in 0.3: every named import that worked in 0.2 still works (`from x import *` is narrower: modules that gained an `__all__` — `cost`, `stats`, `retry`, `scorers.faithfulness` — export only their public names, and `pipeline.calibrate` no longer exports `cluster_kappa` or `kappa_from` by `*`; no registered project uses `*`), and run outputs (summaries,
records, cache keys) are byte-identical. Reports gain an efficiency section (D-67, B5.3) and
runs stopped at the cost cap record `stopped_at_cost_cap` in `run.json`; nothing else in a report changes. Some imports change home
(`python scripts/downstream_imports.py <checkout>` lists yours as `public-elsewhere`):

| 0.2 import | 0.3 public import | Old path |
|---|---|---|
| `evalkit.pipeline.calibrate._Pair(answer, human, judge)` | `evalkit.calibration.CalibrationPair(cluster, human, judge)` | `DeprecationWarning`, removed in 0.4.0 |
| `evalkit.pipeline.calibrate.cluster_kappa` | `evalkit.calibration.cluster_kappa` | `DeprecationWarning`, removed in 0.4.0 |
| `evalkit.pipeline.calibrate.kappa_from` | `evalkit.calibration.kappa_from` | `DeprecationWarning`, removed in 0.4.0 |
| `evalkit.pipeline.answer.SchemaError` | `evalkit.errors.SchemaError` | works, internal |
| `evalkit.pipeline.judge.JUDGE_PROMPT_HASH` | `evalkit.identity.JUDGE_PROMPT_HASH` | works, internal |
| `evalkit.pipeline.extract.EXTRACTOR_VERSION` | `evalkit.identity.EXTRACTOR_VERSION` | works, internal |
| `evalkit.cost`, `evalkit.retry`, `evalkit.stats`, `evalkit.scorers.faithfulness` names above | same path | now public |

`CalibrationPair` takes its fields in `_Pair`'s order, so a positional `_Pair(key, human,
judge)` only changes the class name; `cluster_kappa` and `agreement_table` accept either.

These stay internal because one project imports each (D-61 rule 2); keep the import (the
canary covers it) or use the replacement named in
[the B5.2 spec](../specs/B5.2-public-api/spec.md#single-project-leftovers-migration-guide):

- **agentic-analytics**: `evalkit.cost.UnknownPrice` (catch `KeyError`),
  `evalkit.cost.price_for` (`load_prices()[model_id]`), `evalkit.retry.is_retryable`,
  `evalkit.stats.interval_from`; `evalkit.demo.guard` (named only in its contract test).
- **ai-visibility**: `evalkit.cost.CostEstimate`, `evalkit.cost.CostLine`,
  `evalkit.cost.TokenAssumptions`, `evalkit.pipeline.extract.EXTRACTOR_PROMPT`,
  `evalkit.pipeline.generate.adapter_key`, `evalkit.pipeline.generate.provider_for`,
  `evalkit.pipeline.judge.JUDGE_RUBRIC`, `evalkit.pipeline.judge.SENTENCE_FORMAT`,
  `evalkit.runtime.Executor`, `evalkit.runtime.Store`.
- **course-assistant**: `evalkit.cli`, `evalkit.run`, `evalkit.run.RunError`,
  `evalkit.onboarding`, `evalkit.config.load_task_config`, `evalkit.datasets.load_jsonl_cases`,
  `evalkit.pipeline.aggregate.slice_value`, `evalkit.pipeline.aggregate.summary_json`,
  `evalkit.pipeline.calibrate.KAPPA_DROP_TOLERANCE`, `evalkit.pipeline.calibrate.LabelEntry`,
  `evalkit.pipeline.calibrate.LabelFile`, `evalkit.pipeline.calibrate.MIN_JUDGE_COVERAGE`,
  `evalkit.pipeline.calibrate.SampledAnswer`, `evalkit.pipeline.calibrate._allocate`,
  `evalkit.pipeline.calibrate._opaque`, `evalkit.pipeline.calibrate.build_sheet`,
  `evalkit.pipeline.calibrate.heldout_digest`, `evalkit.pipeline.calibrate.import_labels`,
  `evalkit.pipeline.calibrate.kappa_regressions`, `evalkit.pipeline.calibrate.sheet_csv`,
  `evalkit.pipeline.generate.is_provider_failure`, `evalkit.pipeline.judge`,
  `evalkit.pipeline.judge.redact`, `evalkit.pipeline.labeller.load_labels`,
  `evalkit.pipeline.labeller.save_labels`, `evalkit.pipeline.task_scorers.TaskScorerError`,
  `evalkit.pipeline.task_scorers.resolve_task_scorers`,
  `evalkit.pipeline.task_scorers.score_task` (calibration sheets and labels: B5.9).
- **gemma-grounded-health**: `evalkit.adapters.openai.OpenAICompatibleAdapter`,
  `evalkit.config.ConfigError`, `evalkit.config.load_run_config`,
  `evalkit.pipeline.answer.answer_schema`, `evalkit.pipeline.answer.parse_answer`,
  `evalkit.run.load_cases`, `evalkit.run.load_task`, `evalkit.run.read_cases`,
  `evalkit.run.read_records`, `evalkit.tasks.builtin_task`, `evalkit.tasks.render_prompt`,
  `evalkit.text.number_sentences`, `evalkit.text.split_sentences`.
