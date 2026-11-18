# Moving to EvalKit 0.3.0: a guide per project

This is the migration guide for the seven projects in [downstream.tsv](downstream.tsv)
(D-61, B5.4). Each section lists the project's current pin, the requirement line to use for
0.3.0, every EvalKit import it uses today that is internal or has moved (with its public
replacement), the behaviour changes that affect it, and the capabilities it asked for under
their public names. Each project's own build makes these changes (D-61 rule 4); EvalKit never
edits a downstream repository.

Where the imports come from: `python scripts/downstream_imports.py <checkout>` (parse only, run
on a fresh clone of each project's `main` and on the owner's local checkout on 2026-11-17), the
`imports_not_public` lists in the canary records
([baseline](measurements/downstream-canary-2026-11-17.json),
[release candidate](measurements/downstream-canary-2026-11-17-rc.json)) and
`scripts/check_public_api.py` (`PUBLIC`, `PROMOTED`, `MOVED`, `WITHHELD`).
`tests/test_v03_release.py` checks that every import those records hold is named here, and
that every replacement named in the tables imports and is public.

How to read the tables:

- **public (same path)**: nothing to change; the name is now covered by the versioning policy.
- **moved**: change the import to the public home. Paths marked *warns* raise a
  `DeprecationWarning` in 0.3.x and are removed in 0.4.0; the others keep working silently.
- **internal (keep)**: used by one project only, so it stays internal (D-61 rule 2). Keep the
  import (the canary runs your tests against every EvalKit release candidate) or use the
  alternative named.
- **withheld (D-74)**: not in 0.3.0's public API; see fraud-review-bedrock.

The 0.3.0 requirement line works once the owner has published 0.3.0 to PyPI (V03-03). Until
then, pin the tag: `evalkit-harness @ git+https://github.com/biplob819/evalkit@v0.3.0`.

## Changes that affect every project

- **`summary.json` `efficiency`** (EFF-02, D-70): each model in a new run's `summary.json`
  gains an optional `efficiency` object (tokens per case, cache-hit share, cost per case and
  per correct answer, latency p50/p95/p99 with 95% intervals, error rate, system cost and eval
  overhead, mode, price date). Every other field keeps its bytes. A reader that forbids unknown
  fields must drop it; a byte-for-byte summary fixture must be re-recorded once.
- **One-sided gate bounds** (D-70): `bound: upper` and `bound: lower` in `evalkit gate` use an
  exact one-sided 95% Clopper-Pearson bound for every k (n=150: k=0 → 0.0198, k=1 → 0.0312).
  Displayed intervals stay two-sided.
- **`evalkit compare` `MIN_CASES`**: a change is claimed (enters Holm, can be flagged, can exit
  1) only from at least `MIN_CASES` = 10 shared cases and a non-zero-width interval.
- **`sql_match` details are deterministic**: on a mismatch the first difference reported is
  the same on every run (gold's groups first), and `example_gold_row` is always a gold row (in
  0.2 it could hold a predicted row). Scores are unchanged.
- **Moved calibration paths warn**: `evalkit.pipeline.calibrate._Pair`, `cluster_kappa` and
  `kappa_from` raise a `DeprecationWarning` naming `evalkit.calibration`; a test suite that
  turns warnings into errors fails until the import is changed.
- **value_check is not in 0.3.0** (OWNER D-74): `from evalkit.scorers import value_check` (and
  `ValuePhrase`, `value_check_scorer`, `value_check_shares`, `ValueCheckShares`) raises
  `ImportError`. The strict redesign (B5.16) ships in 0.4.0.
- Also in the 0.3.0 package, documented as 0.4.0 additions in
  [versioning.md](versioning.md#040-additions-on-main-ahead-of-the-release): `evalkit.heldout`
  (B5.14), `evalkit.paraphrase` (B5.8), `evalkit.integrity` (B5.9), `evalkit.security` (B5.11)
  and opt-in `sql_match` `equivalences` (B5.15). The run-history ledger and drift monitor (B5.7)
  are not built yet.

## agentic-analytics

- **Current pin**: `evalkit-harness @ git+https://github.com/biplob819/evalkit@06591f8de9b178078c971940a83019a6530fd119`
- **0.3.0 requirement**: `evalkit-harness>=0.3,<0.4`
- **Release-candidate canary**: pass (238 passed).

| Import today | Class in 0.3.0 | Replace with |
|---|---|---|
| `evalkit.cost.Price` | public (same path) | `evalkit.cost.Price` |
| `evalkit.cost.call_cost` | public (same path) | `evalkit.cost.call_cost` |
| `evalkit.cost.load_prices` | public (same path) | `evalkit.cost.load_prices` |
| `evalkit.retry.with_retries` | public (same path) | `evalkit.retry.with_retries` |
| `evalkit.stats.Resamples` | public (same path) | `evalkit.stats.Resamples` |
| `evalkit.stats.holm` | public (same path) | `evalkit.stats.holm` |
| `evalkit.stats.interval` | public (same path) | `evalkit.stats.interval` |
| `evalkit.stats.paired` | public (same path) | `evalkit.stats.paired` |
| `evalkit.stats.percentile_ms` | public (same path) | `evalkit.stats.percentile_ms` |
| `evalkit.cost.UnknownPrice` | internal (keep) | catch `KeyError` (its base class) around `evalkit.cost.load_prices` lookups |
| `evalkit.cost.price_for` | internal (keep) | `evalkit.cost.load_prices`: `load_prices()[model_id]` (a `KeyError` for an unknown model); keep the import for `free_if_unknown` |
| `evalkit.retry.is_retryable` | internal (keep) | wrap calls in `evalkit.retry.with_retries` |
| `evalkit.stats.interval_from` | internal (keep) | keep; `evalkit.stats.interval` with `evalkit.stats.Resamples` for run-level intervals |
| `evalkit.demo.guard.Guard` | internal (keep) | keep: EvalKit's demo spend guard, named in your contract test; no public form |
| `evalkit.demo.guard.GuardRefused` | internal (keep) | keep (as `Guard`) |
| `evalkit.demo.guard.PostgresSpend` | internal (keep) | keep (as `Guard`) |
| `evalkit.demo.guard.SqliteSpend` | internal (keep) | keep (as `Guard`) |
| `evalkit.demo.guard.Ticket` | internal (keep) | keep (as `Guard`) |
| `evalkit.agents.score.MIN_STEPS` | internal (keep) | keep (local checkout only, not on `main`) |

Behaviour changes that affect it: `sql_match` mismatch details (`example_gold_row` always a
gold row, gold groups first) if a test pins the details; one-sided gate bounds and `MIN_CASES` once it adopts
`evalkit gate` and `evalkit compare`.

Capabilities it asked for (OWNER D-75, D-81):

- Answer-from-rows and clarification scorers (B5.13): `evalkit.scorers.answer_from_rows`,
  `evalkit.scorers.unsupported_numbers`, `evalkit.scorers.clarification_rates`,
  `evalkit.scorers.ClarificationRates`; task scorer names `answer_from_rows` and `asked_back`.
- Gates and compare (B5.3, B5.6): `evalkit.gates.run_gate`, `evalkit.metrics.MetricsSummary`,
  `evalkit.compare.compare_runs`.
- `sql_match` shape equivalences (B5.15, D-71): `evalkit.scorers.sql_match` with
  `equivalences=("percent_scale", "pivot", "empty_check", "period_number")` and `question=`,
  in the package as a 0.4.0 addition; strict by default.
- Run history (B5.7): not in 0.3.0.

## ai-visibility

- **Current pin**: `evalkit-harness @ git+https://github.com/biplob819/evalkit@218192132ad601f02976d486d839e712e39bc4e2`
  (in the `support` dependency group, which `uv sync` installs by default; the registry's
  install column is now `--group support`, it was `--extra support`)
- **0.3.0 requirement**: `evalkit-harness>=0.3,<0.4` (in the same `support` group)
- **Release-candidate canary**: pass (23 passed).

| Import today | Class in 0.3.0 | Replace with |
|---|---|---|
| `evalkit.cost.Price` | public (same path) | `evalkit.cost.Price` |
| `evalkit.cost.call_cost` | public (same path) | `evalkit.cost.call_cost` |
| `evalkit.cost.estimate_tokens` | public (same path) | `evalkit.cost.estimate_tokens` |
| `evalkit.cost.load_prices` | public (same path) | `evalkit.cost.load_prices` |
| `evalkit.scorers.faithfulness.FaithfulnessItem` | public (same path) | `evalkit.scorers.faithfulness.FaithfulnessItem` |
| `evalkit.scorers.faithfulness.FaithfulnessResult` | public (same path) | `evalkit.scorers.faithfulness.FaithfulnessResult` |
| `evalkit.scorers.faithfulness.JudgedClaim` | public (same path) | `evalkit.scorers.faithfulness.JudgedClaim` |
| `evalkit.scorers.faithfulness.faithfulness` | public (same path) | `evalkit.scorers.faithfulness.faithfulness` |
| `evalkit.pipeline.calibrate._Pair` | moved, warns | `evalkit.calibration.CalibrationPair` (same field order: `cluster, human, judge`) |
| `evalkit.pipeline.calibrate.cluster_kappa` | moved, warns | `evalkit.calibration.cluster_kappa` |
| `evalkit.pipeline.calibrate.kappa_from` | moved, warns | `evalkit.calibration.kappa_from` |
| `evalkit.cost.CostEstimate` | internal (keep) | keep; or build estimates from `evalkit.cost.call_cost` and `evalkit.cost.estimate_tokens` |
| `evalkit.cost.CostLine` | internal (keep) | keep (as `CostEstimate`) |
| `evalkit.cost.TokenAssumptions` | internal (keep) | keep (as `CostEstimate`) |
| `evalkit.pipeline.extract.EXTRACTOR_PROMPT` | internal (keep) | pin `evalkit.identity.EXTRACTOR_VERSION` instead of the prompt text |
| `evalkit.pipeline.generate.adapter_key` | internal (keep) | keep; adapters by name with `evalkit.adapters.get_adapter` |
| `evalkit.pipeline.generate.provider_for` | internal (keep) | keep (as `adapter_key`) |
| `evalkit.pipeline.judge.JUDGE_RUBRIC` | internal (keep) | pin `evalkit.identity.JUDGE_PROMPT_HASH` instead of the rubric text |
| `evalkit.pipeline.judge.SENTENCE_FORMAT` | internal (keep) | keep |
| `evalkit.runtime.Executor` | internal (keep) | keep: `faithfulness(executor=...)` needs one; a public runner waits for a second live caller |
| `evalkit.runtime.Store` | internal (keep) | keep (as `Executor`) |

Behaviour changes that affect it: the three calibration paths above warn until changed and are
removed in 0.4.0. `JUDGE_PROMPT_HASH` (9a3a8246049f6f85) and `EXTRACTOR_VERSION`
(`ext2-f484fe5e`) are unchanged by 0.3.0, so its cached judgements stay valid.

Capabilities it asked for (OWNER D-66): the public calibration API at any label unit (PUB-04):
`evalkit.calibration.CalibrationPair`, `evalkit.calibration.agreement_table`,
`evalkit.calibration.AgreementTable`, `evalkit.calibration.cluster_kappa`,
`evalkit.calibration.kappa_from`; faithfulness as a public scorer:
`evalkit.scorers.faithfulness.faithfulness`.

## assay-pipeline

- **Current pin**: `evalkit-harness==0.1.0`
- **0.3.0 requirement**: `evalkit-harness>=0.3,<0.4`
- **Release-candidate canary**: pass (60 passed).

It imports public names only (`evalkit.schemas.AttackBlocked`, `ResultSet`, `Step`,
`Trajectory`, `evalkit.scorers.attack_outcome`, `evalkit.scorers.sql_match`); nothing to
replace.

| Import today | Class in 0.3.0 | Replace with |
|---|---|---|
| `evalkit.scorers.sql_match` | public (same path) | `evalkit.scorers.sql_match` |
| `evalkit.scorers.attack_outcome` | public (same path) | `evalkit.scorers.attack_outcome` |

Behaviour changes that affect it: `sql_match` mismatch details (gold groups first,
`example_gold_row` always a gold row); one-sided 95% Clopper-Pearson gate bounds for every k (D-70; its
pass and fail cases at n=150 hold); `MIN_CASES` in `evalkit compare`.

Capabilities it asked for (OWNER D-62, D-64):

- Gates on any metric with exact bounds and severity (B5.3, GAT-07, GAT-08):
  `evalkit.gates.Gate` (`bound`, `severity: fail|warn`), `evalkit.gates.run_gate`,
  `evalkit.stats.clopper_pearson`, `evalkit.stats.rate_wording`, held-out splits shown only as
  aggregates (`evalkit.metrics.MetricsSummary`).
- Run-vs-run compare (B5.6): `evalkit.compare.compare_runs`, `evalkit.compare.render_markdown`.
- Agent security eval kit (B5.11, built by its session): `evalkit.security.run_attacks`,
  `evalkit.security.load_suite`, `evalkit.security.summarise`, in the package as a 0.4.0
  addition; held-out discipline (B5.14): `evalkit.heldout.build_manifest`.

## course-assistant

- **Current pin**: `evalkit-harness>=0.2,<0.3` (its `eval` extra and its `dev` dependency group; move both)
- **0.3.0 requirement**: `evalkit-harness>=0.3,<0.4` (same extra)
- **Release-candidate canary**: known-drift. Its pin test
  `tests/eval/test_evalkit_contract.py::test_the_released_0_2_line_is_installed_and_pinned` is
  deselected (it checks the installed pin, which the canary replaces). Two release-gate tests
  fail because its committed regression fixture `tests/eval/evalreg/summary.json` is compared
  byte for byte with EvalKit's re-aggregation, which now adds `models[0].efficiency` (D-70; the
  only difference, checked on the RC). Fix in its own build: re-record the fixture under 0.3.0.

| Import today | Class in 0.3.0 | Replace with |
|---|---|---|
| `evalkit.adapters.base.SystemAdapter` | moved | `evalkit.adapters.SystemAdapter` |
| `evalkit.adapters.base.register_system` | moved | `evalkit.adapters.register_system` |
| `evalkit.schemas.benchmark.Answer` | moved | `evalkit.schemas.Answer` |
| `evalkit.schemas.benchmark.Case` | moved | `evalkit.schemas.Case` |
| `evalkit.schemas.benchmark.Record` | moved | `evalkit.schemas.Record` |
| `evalkit.schemas.benchmark.TaskScore` | moved | `evalkit.schemas.TaskScore` |
| `evalkit.schemas.config.TaskConfig` | moved | `evalkit.schemas.TaskConfig` |
| `evalkit.schemas.summary.ModelSummary` | moved | `evalkit.schemas.ModelSummary` |
| `evalkit.schemas.summary.RunSummary` | moved | `evalkit.schemas.RunSummary` |
| `evalkit.pipeline.answer.SchemaError` | moved | `evalkit.errors.SchemaError` |
| `evalkit.pipeline.judge.JUDGE_PROMPT_HASH` | moved | `evalkit.identity.JUDGE_PROMPT_HASH` |
| `evalkit.pipeline.calibrate._Pair` | moved, warns | `evalkit.calibration.CalibrationPair` |
| `evalkit.pipeline.calibrate.cluster_kappa` | moved, warns | `evalkit.calibration.cluster_kappa` |
| `evalkit.pipeline.calibrate.kappa_from` | moved, warns | `evalkit.calibration.kappa_from` |
| `evalkit.cost.Price` | public (same path) | `evalkit.cost.Price` |
| `evalkit.cost.call_cost` | public (same path) | `evalkit.cost.call_cost` |
| `evalkit.cost.estimate_tokens` | public (same path) | `evalkit.cost.estimate_tokens` |
| `evalkit.cost.load_prices` | public (same path) | `evalkit.cost.load_prices` |
| `evalkit.retry.with_retries` | public (same path) | `evalkit.retry.with_retries` |
| `evalkit.stats.Resamples` | public (same path) | `evalkit.stats.Resamples` |
| `evalkit.stats.holm` | public (same path) | `evalkit.stats.holm` |
| `evalkit.stats.interval` | public (same path) | `evalkit.stats.interval` |
| `evalkit.stats.paired` | public (same path) | `evalkit.stats.paired` |
| `evalkit.stats.percentile_ms` | public (same path) | `evalkit.stats.percentile_ms` |
| `evalkit.cli` | internal (keep) | keep; the command line itself and its exit codes are public |
| `evalkit.run` | internal (keep) | keep; the command line and its exit codes are public |
| `evalkit.run.RunError` | internal (keep) | keep; exit code 2 is its public form |
| `import evalkit.onboarding` | internal (keep) | keep |
| `evalkit.config.load_task_config` | internal (keep) | `evalkit.schemas.TaskConfig.model_validate` on the parsed YAML |
| `evalkit.datasets.load_jsonl_cases` | internal (keep) | `evalkit.schemas.Case.model_validate_json` per line |
| `evalkit.pipeline.aggregate.slice_value` | internal (keep) | keep |
| `evalkit.pipeline.aggregate.summary_json` | internal (keep) | `evalkit.schemas.RunSummary.model_dump` with `mode="json"` (a byte-identical file needs the internal writer) |
| `evalkit.pipeline.calibrate.KAPPA_DROP_TOLERANCE` | internal (keep) | keep |
| `evalkit.pipeline.calibrate.LabelEntry` | internal (keep) | keep; criterion sheets: `evalkit.integrity.labels_from_csv` |
| `evalkit.pipeline.calibrate.LabelFile` | internal (keep) | keep (as `LabelEntry`) |
| `evalkit.pipeline.calibrate.MIN_JUDGE_COVERAGE` | internal (keep) | keep |
| `evalkit.pipeline.calibrate.SampledAnswer` | internal (keep) | keep; criterion sheets: `evalkit.integrity.SampledItem` |
| `evalkit.pipeline.calibrate._allocate` | private (keep) | keep; unchanged in 0.3.0 |
| `evalkit.pipeline.calibrate._opaque` | private (keep) | keep; unchanged in 0.3.0 |
| `evalkit.pipeline.calibrate.build_sheet` | internal (keep) | keep; sheets for any criterion: `evalkit.integrity.build_sheet` (different signature) |
| `evalkit.pipeline.calibrate.heldout_digest` | internal (keep) | keep; `evalkit.integrity.heldout_digest` for criterion sheets |
| `evalkit.pipeline.calibrate.import_labels` | internal (keep) | keep |
| `evalkit.pipeline.calibrate.kappa_regressions` | internal (keep) | keep |
| `evalkit.pipeline.calibrate.sheet_csv` | internal (keep) | keep; `evalkit.integrity.sheet_csv` for criterion sheets |
| `evalkit.pipeline.generate.is_provider_failure` | internal (keep) | keep |
| `evalkit.pipeline.judge` | internal (keep) | keep; read the hash from `evalkit.identity.JUDGE_PROMPT_HASH` |
| `evalkit.pipeline.judge.redact` | internal (keep) | keep |
| `evalkit.pipeline.labeller.load_labels` | internal (keep) | keep |
| `evalkit.pipeline.labeller.save_labels` | internal (keep) | keep |
| `evalkit.pipeline.task_scorers.TaskScorerError` | internal (keep) | keep; exit code 2 is its public form |
| `evalkit.pipeline.task_scorers.resolve_task_scorers` | internal (keep) | keep |
| `evalkit.pipeline.task_scorers.score_task` | internal (keep) | keep |

Behaviour changes that affect it: `summary.json` `efficiency` (re-record the evalreg fixture
once; nothing else in the summary changes); the three calibration paths warn; one-sided gate
bounds; `MIN_CASES` in compare.

Capabilities it asked for (OWNER D-53, D-66, D-68; EvalKit D-65, D-69):

- Release-gate rigour on EvalKit's exit codes 2 > 3 > 4 > 1 > 0 (GAT-09):
  `evalkit.gates.run_gate`, `evalkit.gates.EXIT_PASS`, `evalkit.gates.EXIT_FAIL`,
  `evalkit.gates.EXIT_INPUT`, `evalkit.gates.EXIT_COST_CAP`, `evalkit.gates.EXIT_PENDING`.
- Latency and cost gates (GAT-10): the standard names in `evalkit.metrics.EFFICIENCY_NAMES`
  (`latency_p95`, `cost_per_case`, ...) from `evalkit.metrics.from_run_summary`.
- Baselines that never skip silently, case-only resampling and a per-case sidecar (RVR-03):
  `evalkit.compare.compare_runs`, `evalkit.compare.paired_bootstrap`.
- Held-out manifest, score-once guard, exposure scan and tuning log (B5.14):
  `evalkit.heldout.build_manifest`, `evalkit.heldout.record_scoring`,
  `evalkit.heldout.exposure_scan`; calibration sheets for any criterion (B5.9):
  `evalkit.integrity.build_sheet`, `evalkit.integrity.score_sheet`. Both are in the package as
  0.4.0 additions.
- Monitoring and release ledger (B5.7): not in 0.3.0.

## fraud-review-bedrock

- **Current pin**: `evalkit-harness @ git+https://github.com/biplob819/evalkit@4ddfef50d55db653aaf57048af06f2ba606e1573` (with extras `anthropic` and `openai`)
  (the v0.3.0 tag commit; its suite passes on it, 1321 tests. It was the release candidate 118b661, and add5af9 before that)
- **0.3.0 requirement**: `evalkit-harness>=0.3,<0.4` (move from the release-candidate commit to the tag).
- **Release-candidate canary**: pass (1264 passed) on its e41ddc9, which moved its bridge test and
  doctor check to its own strict `valuecheck` module (its D-42) and pins the release candidate.
  Before that change the suite stopped at collection on the withheld `ValuePhrase` (D-74).

It imported these before e41ddc9; the value-check names now come from its own module:

| Import today | Class in 0.3.0 | Replace with |
|---|---|---|
| `evalkit.scorers.value_check` | withheld (D-74) | none public in 0.3.0: keep your own checker until 0.4.0 (B5.16), as D-74 says; judge reasons against the record with `evalkit.scorers.render_evidence` and `evalkit.scorers.faithfulness.faithfulness` |
| `evalkit.scorers.ValuePhrase` | withheld (D-74) | none public in 0.3.0 (as `value_check`); declare your phrases in your own checker |
| `evalkit.scorers.faithfulness.FaithfulnessItem` | public (same path) | `evalkit.scorers.faithfulness.FaithfulnessItem` |
| `evalkit.scorers.faithfulness.faithfulness` | public (same path) | `evalkit.scorers.faithfulness.faithfulness` |
| `evalkit.scorers.rule_compliance` | public (same path) | `evalkit.scorers.rule_compliance` |
| `evalkit.scorers.render_evidence` | public (same path) | `evalkit.scorers.render_evidence` |

The code it imports today still exists in the internal module `evalkit.scorers.evidence`, but
it is the design that D-74 withdrew (some false claims came back `verified`) and it may change
in any release; do not gate on it. Its doctor check pins the EvalKit version
(`EVALKIT_VERSION`), so that constant changes with the pin.

Behaviour changes that affect it: D-74 above; `MIN_CASES` and paired intervals in
`evalkit compare` for its drift check; the moved calibration paths if it adopts EvalKit's
calibration.

Capabilities it asked for (OWNER D-36, D-38):

- Rule compliance and structured evidence (B5.13): `evalkit.scorers.Rule`,
  `evalkit.scorers.RuleSet`, `evalkit.scorers.RuleError`, `evalkit.scorers.rule_compliance`,
  `evalkit.scorers.rule_compliance_scorer`, `evalkit.scorers.render_evidence`.
- Faithfulness, calibration, paired bootstrap with Holm: `evalkit.scorers.faithfulness.faithfulness`,
  `evalkit.calibration.agreement_table`, `evalkit.stats.paired`, `evalkit.stats.holm`.
- Drift check (B5.6): `evalkit.compare.compare_runs`.
- Strict value checks (B5.16) and the monitoring and improvement ledger (B5.7): 0.4.0.

## gemma-grounded-health

- **Current pin**: `evalkit-harness[openai,anthropic] @ git+https://github.com/biplob819/evalkit@06591f8de9b178078c971940a83019a6530fd119`
- **0.3.0 requirement**: `evalkit-harness[openai,anthropic]>=0.3,<0.4`
- **Release-candidate canary**: known-drift. `tests/evals/test_config.py::test_spec_covers_every_run_config_field`
  fails because `RunConfig` has had a `systems` field since 0.2.0 that its config spec does not
  name. Its two pin tests are deselected (they check the installed pin).

| Import today | Class in 0.3.0 | Replace with |
|---|---|---|
| `evalkit.adapters.base.CompletionRequest` | moved | `evalkit.adapters.CompletionRequest` |
| `evalkit.adapters.base.RawCompletion` | moved | `evalkit.adapters.RawCompletion` |
| `evalkit.pipeline.answer.SchemaError` | moved | `evalkit.errors.SchemaError` |
| `evalkit.pipeline.extract.EXTRACTOR_VERSION` | moved | `evalkit.identity.EXTRACTOR_VERSION` |
| `evalkit.pipeline.judge.JUDGE_PROMPT_HASH` | moved | `evalkit.identity.JUDGE_PROMPT_HASH` |
| `evalkit.adapters.openai.OpenAICompatibleAdapter` | internal (keep) | keep; or an adapter by name with `evalkit.adapters.get_adapter` |
| `evalkit.config.ConfigError` | internal (keep) | keep |
| `evalkit.config.load_run_config` | internal (keep) | `evalkit.schemas.RunConfig.model_validate` on the parsed YAML |
| `evalkit.pipeline.answer.answer_schema` | internal (keep) | keep |
| `evalkit.pipeline.answer.parse_answer` | internal (keep) | keep; catch `evalkit.errors.SchemaError` |
| `evalkit.run.load_cases` | internal (keep) | keep |
| `evalkit.run.load_task` | internal (keep) | keep |
| `evalkit.run.read_cases` | internal (keep) | `evalkit.schemas.Case.model_validate_json` per line of `cases.jsonl` |
| `evalkit.run.read_records` | internal (keep) | `evalkit.schemas.Record.model_validate_json` per line of `records.jsonl` |
| `evalkit.tasks.builtin_task` | internal (keep) | keep |
| `evalkit.tasks.render_prompt` | internal (keep) | keep |
| `evalkit.text.number_sentences` | internal (keep) | keep |
| `evalkit.text.split_sentences` | internal (keep) | keep |
| `evalkit.cost.price_for` | internal (keep) | `evalkit.cost.load_prices`: `load_prices()[model_id]` (local checkout only) |
| `evalkit.datasets.pubmedqa.load_pqa_l` | internal (keep) | keep (local checkout only) |
| `evalkit.sampling.stratified_sample` | internal (keep) | keep (local checkout only) |

Behaviour changes that affect it: add `systems` to its config spec (`config.SPEC`) in the same
change as the pin (OWNER D-67), which turns its known-drift test green; its
`docs/results/main/summary.json` is EvalKit's format, and new runs add a per-model
`efficiency` object (0.3's `evalkit.schemas.ModelSummary` reads it; a strict reader of its own
must allow it). `JUDGE_PROMPT_HASH` (9a3a8246049f6f85) and `EXTRACTOR_VERSION` are unchanged.

Capabilities it asked for (OWNER D-42, D-44):

- Per-sentence citation support (B5.12): list `citation_support` among a task's scorers; the
  results are `evalkit.schemas.CitationVerdict`, `evalkit.schemas.Record` field
  `citation_support`, and `citation_precision` and `aim_citation_rate` on
  `evalkit.schemas.ModelSummary` and `evalkit.schemas.SliceSummary`.
- Paraphrase contamination probe (B5.8, built from its own set): `evalkit.paraphrase.load_set`,
  `evalkit.paraphrase.contamination_report`, in the package as a 0.4.0 addition.

## respectful-offers

- **Current pin**: `evalkit-harness>=0.3,<0.4` (its `evalkit` extra and dev group), adopted at its
  commit 7d595ef (its E4.2); the canary runs `tests/evalkit/test_evalkit_contract.py` (43 offline
  contract tests).
- **0.3.0 requirement**: `evalkit-harness>=0.3,<0.4` (met). It imports only `evalkit.metrics` (`load_metrics`, `parse_metrics`,
  `MetricsError`) and `evalkit.gates` (`run_gate`, `EXIT_PASS`, `EXIT_INPUT`), enforced by its ast
  test. Its stricter validator refused id-shaped label values; EvalKit refuses them too since
  0.4.0. Equal-width reliability bins (D-73) come with its E4.3.

| Import today | Class in 0.3.0 | Replace with |
|---|---|---|
| (none) | | |

Capabilities it asked for (OWNER D-63, D-67):

- Its metrics-1 exporter (B5.3, GAT-05: multi-field labels, a unit per metric such as NTD,
  supplied intervals with their method, exact counts, the `simulated` flag):
  `evalkit.metrics.MetricsSummary`, `evalkit.metrics.Metric`, `evalkit.metrics.Provenance`,
  `evalkit.metrics.load_metrics`; gates with `evalkit.gates.run_gate`, rule-violation counts
  gated `== 0`.
- Group disparity with a max-gap gate (GAT-06): `evalkit.gates.disparity_gap`.
- Run-vs-run compare (B5.6): `evalkit.compare.compare_runs`.
- Drift and calibration monitor (B5.7, LED-04): not in 0.3.0.

**An ask from EvalKit (OWNER D-73):** please export **equal-width reliability bins** (edges
0.0, 0.1, ..., 1.0) with your calibration figures. Until you do, EvalKit's drift monitor
compares only your Brier score and ECE and reports per-bin drift as not available; it never
compares quantile bins whose edges move between runs.
