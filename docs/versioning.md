# Versioning policy

EvalKit follows [Semantic Versioning](https://semver.org/). Before 1.0.0, a minor release
(0.x → 0.y) may break the public API, and the changelog says how to migrate; patch releases
(0.x.y) never do.

## What is public

These are covered by the policy, and `tests/test_public_api.py` fails if one disappears:

- **The command line**: `evalkit` subcommands, their options and the exit codes.
- **`evalkit.scorers`**: every name in its `__all__` (the scorer functions, `scorer`,
  `Scorer`, `ScoreResult`, `discover`, `get_scorer`); since 0.4.0 this includes `value_check`,
  `value_check_scorer`, `value_check_shares`, `ValuePhrase` and `ValueCheckShares` (B5.16,
  D-77), which 0.3.0 withheld (D-74).
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
  Since 0.4.0 also `TokenAssumptions` (ai-visibility and fraud-review-bedrock) and
  `CostEstimate` with its `CostLine`s, which `estimate_faithfulness_cost` returns.
- **`evalkit.retry`** (since 0.3.0): `with_retries`.
- **`evalkit.stats`** (since 0.3.0): `Resamples`, `interval`, `paired`, `holm`,
  `percentile_ms`; and (B5.3) `clopper_pearson`, `rate_wording`.
  Since 0.4.0 also `two_level_interval` (clustered intervals, agentic-analytics D-100) and
  `interval_from` (agentic-analytics and fraud-review-bedrock).
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
  values a current `Record` carries as `judge_prompt_hash` and `extractor_version`. Since 0.4.0
  also `JUDGE_RUBRIC` (ai-visibility and fraud-review-bedrock), the rubric text the hash covers:
  size prompts with it, pin the hash.
- **`evalkit.runtime`** (since 0.4.0, ai-visibility and fraud-review-bedrock):
  `Executor` (`Executor(store, *, concurrency=None, default_concurrency=4, max_cost_usd=None,
  ...)`, what `faithfulness(executor=...)` takes), `Store` (the content-addressed cache it
  runs on) and `BudgetExceeded`, which it raises when the next call could pass
  `max_cost_usd`. The module's other names stay internal.
- **`evalkit.providers`** (since 0.4.0, ai-visibility and fraud-review-bedrock):
  `provider_for` (`provider_for(model_id, endpoints=None, systems=None)`), which adapter serves
  a model (`anthropic`, `openai`, `openai_compatible` or `system`); the object EvalKit's run
  uses.
- **`evalkit.compare`** (since 0.3.0, B5.6): every name in its `__all__` (run-vs-run compare
  and `evalkit compare`; see [compare.md](compare.md)).
- **Claims per item** (0.4.0, fraud-review-bedrock, D-61 rule 3): in `evalkit.cost`,
  `ClaimsAssumption`, `claims_from_texts` and `measured_claims`; in
  `evalkit.scorers.faithfulness`, `estimate_faithfulness_cost`; the run command's
  `--claims-per-answer` option. See [cost.md](cost.md#claims-per-answer).

The modules that shipped in 0.4.0 (`evalkit.heldout`, `evalkit.paraphrase`,
`evalkit.integrity`, `evalkit.security`, `evalkit.ledger`, `evalkit.drift`) are public by
their `__all__`; see [0.4.0 additions](#040-additions).

Everything else, including `evalkit.pipeline`, the rest of `evalkit.runtime` and
`evalkit.demo`, is internal and may change in any release.

## Results format

`summary.json` and the per-case records carry `schema_version`. A field is never renamed or
removed within a major version of the format; new optional fields may appear in a minor
release. A reader should ignore fields it does not know.

0.2.0 added optional fields (`Case.meta`, `Record.task_scores`, `ModelSummary.task_scores`,
`RunSummary.slices`, `RunConfig.systems`, `TaskConfig.slices`). Each is left out of the JSON
while it is empty, so a run that uses none of them writes exactly what 0.1.0 wrote and 0.1.0
can read it; a run that uses them needs 0.2.0 or later to read its records and summary.

## 0.3.0 additions

> **Provisional in 0.3.0, resolved in 0.4.0.** `evalkit.heldout`, `evalkit.paraphrase` (and the `--paraphrase` option of the report command), `evalkit.integrity` (and the `--criterion` option of the calibrate command), `evalkit.security` and the `sql_match` `question=`/`equivalences=` options were already in the 0.3.0 wheel because they were green on main, and 0.3.0 marked them provisional: their nodes shipped formally in 0.4.0 (D-62). Since 0.4.0 they are covered by the usual one-minor-version deprecation window, like everything else listed here; see [0.4.0 additions](#040-additions).

- `evalkit.scorers`: `answer_from_rows`, `unsupported_numbers`, `clarification_rates`, `ClarificationRates`, `Rule`, `RuleSet`, `RuleError`, `rule_compliance`, `rule_compliance_scorer` and `render_evidence` (B5.13, D-65). `value_check`, `value_check_scorer`, `value_check_shares`, `ValuePhrase` and `ValueCheckShares` are **not** public in 0.3.0 (OWNER D-74): they stay in the internal module `evalkit.scorers.evidence` and may change in any release until their strict redesign (B5.16) ships in 0.4.0; `scripts/check_public_api.py` fails if `evalkit.scorers` exports them. They are public since 0.4.0 ([plugins.md](plugins.md#value_check-a-structured-facts-check-040-d-74-d-77)).
- `evalkit.schemas.CitationVerdict` and the optional fields `Record.citation_support`, `ModelSummary.citation_precision` and `ModelSummary.aim_citation_rate`, and the same two on `SliceSummary` (B5.12, D-65), each omitted from JSON when empty.

- The `evalkit gate` command (B5.3, D-62..D-65), with a summary file and the options `--config`, `--json` and `--out`: gates on any named metric from a `metrics-1` file or an EvalKit `summary.json`; exit codes 0/1/2/3/4 with precedence 2 > 3 > 4 > 1 > 0; no model or network call. See [gates.md](gates.md).
- `evalkit.metrics` (B5.3): the frozen `metrics-1` format (`MetricsSummary`, `Metric`, `Provenance`, `parse_metrics`, `load_metrics`, `MetricsError`, `SCHEMA`), the standard efficiency names (`EFFICIENCY_NAMES`, D-67), `from_run_summary` and `efficiency_metrics`, and `safe_text`.
- `evalkit.gates` (B5.3): `Gate`, `GatesFile`, `parse_gates`, `load_gates`, `evaluate`, `run_gate`, `GateReport`, `GateRow`, `RunStatus`, `disparity_gap`, `render_text`, the input-classes table `INPUTS` and the exit-code constants.
- `evalkit.stats.clopper_pearson` and `evalkit.stats.rate_wording` (B5.3, GAT-07): exact binomial intervals, two-sided by default and one-sided with `sided="upper"` or `sided="lower"` (the bounds gates check, D-70), and the "0 of n, below X% at 95%" wording.
- `summary.json` gains an optional per-model `efficiency` object (EFF-02, OWNER D-70): token means per case, cache-hit share, cost per case and per correct answer and latency p50/p95/p99 with seeded 95% bootstrap intervals, error rate with its count, system cost and eval overhead (extractor and judges), mode and price date; `retry_rate` is null (not recorded). It is omitted when absent, so a summary without it reads as before. The run report gains an Efficiency section with the same figures, and shares that are exactly 0 or 1 read "0 of n, below X% at 95%" instead of a zero-width interval. See [gates.md](gates.md#efficiency-in-summaryjson-03-eff-02-d-70). `run.json` gains `stopped_at_cost_cap`, written only when a run stops at its cap (omitted otherwise, so run folders keep their bytes).
- The `evalkit compare` command and `evalkit.compare` (B5.6, D-62, D-65): per metric and label, the change new − old with a 95% interval (paired two-level bootstrap on shared case ids, case-only without a trial level, otherwise unpaired from each run's interval and labelled so), Holm-adjusted flags, the settings that differ, missing metrics listed, held-out rows as split-level aggregates only, an optional per-case `cases.jsonl` sidecar beside a metrics-1 file and an optional `settings.json`; options `--gates`, `--manifest`, `--heldout-split`, `--seed`, `--resamples`, `--json`, `--out` and `--md`; exit codes 0/1/2; no model or network call. See [compare.md](compare.md).

## 0.4.0 additions

- `value_check`, `value_check_scorer`, `value_check_shares`, `ValuePhrase` and `ValueCheckShares` from `evalkit.scorers` (B5.16, D-74, D-77): withheld from 0.3.0, public again as a strict structured-facts check. A reason is `verified` only when every number in it is bound by a framed "field is value" statement (a facts line such as "Facts: Amount is 812.40. Hour is 3.", last in the reason or after a facts header); every other reading, prose included, is `unverifiable` (no false claim verified across about 450,000 generated cases, D-77). See [plugins.md](plugins.md#value_check-a-structured-facts-check-040-d-74-d-77).
- Names two or more projects import, promoted under D-61 rule 2 (listed under [What is public](#what-is-public)): `evalkit.cost.TokenAssumptions`, `CostEstimate`, `CostLine`; `evalkit.stats.interval_from`; `evalkit.identity.JUDGE_RUBRIC`; `evalkit.runtime.Executor`, `Store`, `BudgetExceeded`; `evalkit.providers.provider_for`. The old paths keep working silently.
- Judging completeness (D-78): `Record.judge_errors` (per judge, the claims it finally failed to rule on) and `ModelSummary.missing_verdicts` (per judge, the claims with no verdict), both optional and omitted when empty.

- `evalkit.heldout` (B5.14, D-65): held-out manifest of hashes and counts, score-once guard, exposure scan and hash-chained append-only log; `python -m evalkit.heldout manifest|verify|scan|verify-log` (exit codes 0, 1, 2). See [heldout.md](heldout.md).
- `evalkit.paraphrase` (B5.8, D-68): the paraphrase contamination probe set, its builder and the original-vs-paraphrase report; built-in tasks `pubmedqa-paraphrase` and `pubmedqa-paraphrase-original`; `evalkit report --paraphrase ORIGINAL_RUN PARAPHRASE_RUN`; `python -m evalkit.paraphrase verify|build` (exit codes 0, 2, 3). See [contamination.md](contamination.md).
- `evalkit.integrity` (B5.9, D-69): calibration sheets for any declared two-way criterion (opaque keys per export, labeller-only context pane, per-judge held-out kappa with a bootstrap CI) and labeller consistency (blind repeats and planted items in a second pass), certified only through a `calibration_scored` line in the B5.14 log; the `evalkit calibrate` subcommands `export`, `label`, `import` and `score` take `--criterion FILE`, with `--manifest`, `--planted`, `--context`, `--relabel`, `--log` and `--rubric-hash`. See [calibrate-your-task.md](calibrate-your-task.md#calibrate-any-criterion).
- `sql_match` keyword arguments `question=None` and `equivalences=()` (B5.15, D-71): opt-in shape equivalences `percent_scale`, `pivot`, `empty_check`, `period_number`, tried only after a strict miss; with any named, `details` gains `correct_strict`, `equivalences` and (on a rescue) `strict_reason`. With none named the result is unchanged. See [plugins.md](plugins.md#sql_match-shape-equivalences-040-b515).
- `evalkit.security` (B5.11, D-64, D-70): the agent security eval kit — attack-case schema and suites with held-out cases pinned by a B5.14 manifest, k trials and pass^k, configurable stopping-layer attribution, a model-free scripted attacker, split-level summary, metrics-1 output for `evalkit gate`, report and compare; `python -m evalkit.security check|run|scripted|report` (exit codes 0, 1, 2). See [security.md](security.md).
- Claims per item behind every judge estimate (fraud-review-bedrock, D-61 rule 3): `evalkit.cost.ClaimsAssumption`, `claims_from_texts` (claims counted from texts that already exist, no model call: each item at the largest of its words ÷ 8, sentence-like units, key=value fields, distinct numbers, characters ÷ 50 and the planning default 4.5; defaults and overrides must be finite, above 0 and at most 10,000) and `measured_claims` (an explicit figure from a small sample); `evalkit.scorers.faithfulness.estimate_faithfulness_cost`; the run command's `--claims-per-answer N` option; the dry run prints the assumption and its `--json` gains a top-level `claims` object. `CostEstimate` gains `claims`, left out of its JSON while it is the planning default, so existing estimates keep their bytes. See [cost.md](cost.md#claims-per-answer).
- Clustered intervals (agentic-analytics OWNER D-100, L-36): `evalkit.stats.two_level_interval` (the two-level bootstrap `evalkit compare` uses, resample cases then trials, now public); `clarification_rates(..., cluster=, seed=0, n_resamples=10_000)` switches both intervals to it (`method: cluster-bootstrap`, with the question counts, seed and resample count, each left out of the JSON otherwise) and `ClarificationRates.ci_method`; `evalkit.security.summarise(..., cluster_trials=True)` puts the same kind of interval on `attack_trial_success_rate` in metrics-1. Every default is byte-identical. See [plugins.md](plugins.md) and [security.md](security.md#outputs-and-gates).
- Strict run-folder detection: a `run.json` or `summary.json` is EvalKit's only when it carries EvalKit's `schema_version` and the keys EvalKit always writes; `evalkit compare`, `evalkit gate`, `evalkit report` and a resumed run name why another project's file is not an EvalKit run instead of reading it as one. See [compare.md](compare.md#what-counts-as-an-evalkit-run-folder-040).
- `evalkit.ledger` and `evalkit.drift` (B5.7, D-62, D-63, D-65, D-67, D-73): hash-chained run, experiment and spend ledgers through the B5.14 writer; alarm bands per metric from each project's own history (median ± 2/3 spreads, at least 5 comparable runs), the efficiency spread table and the trend page; an experiment ledger whose freeze guard reserves one held-out look per frozen candidate in the B5.14 log; a spend cap alarm (legacy lines sealed by the first chained line); PSI by fraud-review-bedrock's rule and Brier/ECE/reliability-bin drift with 95% intervals. The `evalkit` subcommands `history` (`add`, `check`, `show`, `reset`, `verify`), `experiment` (`open`, `close`, `look`, `show`), `drift` (`reference`, `check`) and `spend` (`add`, `check`) (each with `--json`; exit codes 0, 1, 2, 3, 4, precedence 2 > 3 > 4 > 1 > 0) and `evalkit report --history LEDGER`; `smoke.record_spend` keeps its signature and return value and now writes a chained line. See [ledger.md](ledger.md) and [drift.md](drift.md).

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

Nothing breaks in 0.3: every named import that worked in 0.2 still works (`from x import *` is narrower: modules that gained an `__all__` — `cost`, `stats`, `retry`, `scorers.faithfulness` — export only their public names, and `pipeline.calibrate` no longer exports `cluster_kappa` or `kappa_from` by `*`; no registered project uses `*`), and run outputs (records,
cache keys) are byte-identical. `summary.json` gains one optional field per model, `efficiency` (D-70); every other field keeps its bytes, and a 0.2 reader that forbids unknown fields must drop it (`ModelSummary` in 0.3 reads it). Reports gain an
efficiency section (D-67, B5.3) and the GAT-07 wording for shares of exactly 0 or 1, and runs stopped at the cost cap record
`stopped_at_cost_cap` in `run.json`; nothing else in a report changes. Some imports change home
(`python scripts/downstream_imports.py <checkout>` lists yours as `public-elsewhere`):

| 0.2 import | 0.3 public import | Old path |
|---|---|---|
| `evalkit.pipeline.calibrate._Pair(answer, human, judge)` | `evalkit.calibration.CalibrationPair(cluster, human, judge)` | `DeprecationWarning`, removed in 0.5.0 (was 0.4.0) |
| `evalkit.pipeline.calibrate.cluster_kappa` | `evalkit.calibration.cluster_kappa` | `DeprecationWarning`, removed in 0.5.0 (was 0.4.0) |
| `evalkit.pipeline.calibrate.kappa_from` | `evalkit.calibration.kappa_from` | `DeprecationWarning`, removed in 0.5.0 (was 0.4.0) |
| `evalkit.pipeline.answer.SchemaError` | `evalkit.errors.SchemaError` | works, internal |
| `evalkit.pipeline.judge.JUDGE_PROMPT_HASH` | `evalkit.identity.JUDGE_PROMPT_HASH` | works, internal |
| `evalkit.pipeline.extract.EXTRACTOR_VERSION` | `evalkit.identity.EXTRACTOR_VERSION` | works, internal |
| `evalkit.cost`, `evalkit.retry`, `evalkit.stats`, `evalkit.scorers.faithfulness` names above | same path | now public |

Each registered project's exact list of changes, with its 0.3.0 requirement line, is in
[downstream.md](downstream.md).

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

## 0.4.0 → 0.4.1

Additive. One byte-level change: `drift-ref-1` and `drift-1` now record `simulated` (respectful-offers), so a drift reference frozen again on 0.4.1 has a new `profile_sha256` for the same input; every figure and level is unchanged, and a reference frozen on 0.4.0 still loads and checks (tests/test_drift.py). A project that commits a frozen reference and compares it byte for byte should re-freeze it once on 0.4.1.

## 0.3 → 0.4 migration

Every named import that worked in 0.3 still works in 0.4, and a run that uses none of the
changes below writes the same records, cache keys and `summary.json` bytes (`evalkit run`
still plans 4.5 claims per answer, so its goldens are unchanged). These change behaviour:

1. **Claims per item in estimates (D-76).** When the texts to judge already exist
   (faithfulness on given explanations, re-judging saved answers), an estimate plans each text
   at the largest of its words ÷ 8, sentence-like units, key=value fields, distinct numbers,
   characters ÷ 50 and 4.5, instead of 4.5 claims for every item. Quotes for long
   explanations rise (fraud-review-bedrock's were about half the real cost) and can now reach
   a cap that a 0.3 quote did not. Every estimate states its assumption and its source; the
   dry run's `--json` gains a top-level `claims` object; `CostEstimate` gains `claims`, left out
   of its JSON while it is the planning default. Pass a measured figure with
   `--claims-per-answer N` or `claims_per_item=`. See [cost.md](cost.md#claims-per-answer).
2. **Judging is complete only when every claim is ruled on by every judge (D-78).** A claim
   with no verdict (a batch item that came back empty, or whose retry hit a provider error)
   keeps its judge pending, so the run exits 4 and a rerun resends only those claims; in 0.3
   such a run could say `judging_status: complete` with groundedness computed over the claims
   that had verdicts. A final failure (a refusal, or a reply still invalid after the one retry)
   is recorded in `Record.judge_errors`, counted in `ModelSummary.missing_verdicts` and scored
   unsupported, never left out of the denominator. Both fields are omitted when empty. A batch
   blocked only by another judge's open hold now waits within `--wait` instead of stopping at
   the cap; nothing is ever reserved past it.
3. **Id-shaped label values are refused in metrics-1.** A label value shaped like a case or
   member id (a uuid, 32 or more hex characters, an email, or a base64-like token of 20 or
   more characters mixing upper case, lower case and digits) is a parse error, exit 2 in
   `evalkit gate` and `evalkit compare`, as id-named label keys already were. Model ids,
   category codes and years are unaffected (respectful-offers' D-53).
4. **Strict run-folder detection.** A `run.json` or `summary.json` is EvalKit's only when it
   carries EvalKit's `schema_version` and every key EvalKit always writes there. `evalkit
   compare`, `evalkit gate`, `evalkit report` and a resumed run say why another project's file
   is not an EvalKit run instead of reading it as one; compare reads such a folder through its
   `metrics.json` when it has one. See
   [compare.md](compare.md#what-counts-as-an-evalkit-run-folder-040).
5. **`--out` may name a folder.** `evalkit gate SUMMARY --out DIR` writes `DIR/gate.json` and
   `evalkit compare OLD NEW --out DIR` / `--md DIR` write `compare.json` / `compare.md` inside it (a
   path that is an existing folder or ends with a path separator); 0.3 exited 2 for a folder.
6. **`sql_match` details.** The opt-in equivalences (`question=`, `equivalences=`) and the
   details they add (`correct_strict`, `equivalences`, and `strict_reason` on a rescue) were
   provisional in 0.3.0 and are covered from 0.4.0. With none named, scores and details are
   exactly 0.3.0's.
7. **`value_check` is public and strict (D-74, D-77).** `from evalkit.scorers import
   value_check` works again. It is not the checker 0.3.0 withheld: `verified` only when every
   number in a reason is bound by a framed "field is value" statement of the
   [facts grammar](plugins.md#value_check-the-facts-grammar-verify516c) (`tolerance` defaults
   to 0.0; `units=` declares each field's unit), and everything else, prose included, is
   `unverifiable`. On fraud-review-bedrock's 10,225 prose reasons 4 verify; a facts line at the
   end of each reason ("Facts: Amount is 812.40. Hour is 3.") verifies reliably. Code that used
   the internal `evalkit.scorers.evidence` in 0.3 gets the strict check too.

Also: the moved calibration paths (`evalkit.pipeline.calibrate._Pair`, `cluster_kappa`,
`kappa_from`) still work in 0.4 with their `DeprecationWarning`; they were due to go in 0.4.0,
and are kept one more minor release because two registered projects still import them (the
[2026-11-18 canary](measurements/downstream-canary-2026-11-18.json)). They are removed in 0.5.0.
`evalkit.runtime`'s `__all__` now lists only its public names (`Executor`, `Store`,
`BudgetExceeded`), so `from evalkit.runtime import *` is narrower; named imports are
unchanged, and no registered project uses `*`.

Names promoted under D-61 rule 2 (each used by two registered projects in the 2026-11-18
canary record and the owner's local checkouts):

| 0.3 import | 0.4 public import | Projects | Old path |
|---|---|---|---|
| `evalkit.cost.TokenAssumptions` | `evalkit.cost.TokenAssumptions` | ai-visibility, fraud-review-bedrock | same path |
| `evalkit.cost.CostEstimate`, `evalkit.cost.CostLine` | same path | ai-visibility (returned by `estimate_faithfulness_cost`) | same path |
| `evalkit.stats.interval_from` | `evalkit.stats.interval_from` | agentic-analytics, fraud-review-bedrock | same path |
| `evalkit.runtime.Executor` | `evalkit.runtime.Executor` | ai-visibility, fraud-review-bedrock | same path |
| `evalkit.runtime.Store`, `evalkit.runtime.store.Store` | `evalkit.runtime.Store` | ai-visibility, fraud-review-bedrock | works, internal |
| `evalkit.runtime.executor.BudgetExceeded` | `evalkit.runtime.BudgetExceeded` | fraud-review-bedrock (raised by `Executor`) | works, internal |
| `evalkit.pipeline.generate.provider_for` | `evalkit.providers.provider_for` | ai-visibility, fraud-review-bedrock | works, internal |
| `evalkit.pipeline.judge.JUDGE_RUBRIC` | `evalkit.identity.JUDGE_RUBRIC` | ai-visibility, fraud-review-bedrock | works, internal |

Each registered project's exact list for 0.4, with its 0.4.0 requirement line, is in the
"0.3 → 0.4" part of its section in [downstream.md](downstream.md).

## Additions after 0.4.0 (unreleased)

- `evalkit.index`, `evalkit index snapshot` and `evalkit index build` (B5.5, D-61, D-62,
  D-67): the
  cross-project eval index. `snapshot` (the one step that may reach a remote) fetches each
  registered project's summary at one commit into a local snapshot folder; `build` reads only
  local files and writes `index.html` and `index.json` (`index-1`, byte-reproducible): per
  project the headline row with its interval, the calibration stamp, the gate status, the EvalKit
  version and the run date; a project whose summary cannot be read shown as missing with the
  reason; per model the efficiency block beside quality, ordered by cost per correct; the
  portfolio's `story` keys. Exit codes 0/1/2. Every name in the module's `__all__` is public.
  See [index.md](index.md). Its config also takes `pointer` (a file naming the current results
  folder), `own_format` with path selectors (`$.field`, `[?field == value && ...]`, `per`
  fan-out) for a project's own summary format, `comparison`, and per selector `direction`,
  `decimals` and a `companion` row.
- `evalkit.drift.to_metrics` carries `simulated` from the new batch's metrics-1 file, else the
  reference's source file (never stated when neither states it).
- `evalkit.metrics.from_run_summary`: the model-level `error_rate` row of a summary written
  before 0.3 is labelled `{model}` only. It was labelled `mode: live`, which the summary never
  records. A gate on `error_rate {model: X, mode: live}` over such a summary names `{model: X}`
  now; rows from an efficiency block keep the mode the block records.
