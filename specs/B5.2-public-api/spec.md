# B5.2 — Public API 0.3: promote what downstream uses

**Wave 5. Depends on:** B5.1, S2.4. Decisions: D-61 to D-66.

Skeleton written by the lead so the lane can start (D-66); completed by the lane's builder from
the B5.1 canary (`docs/measurements/downstream-canary-2026-11-17.json`, `imports_not_public` per
project) and `inputs.md`.

## Capabilities
- **PUB-01**: Internals used by two or more projects get public homes listed in `docs/versioning.md` and `tests/test_public_api.py` [D-61]
- **PUB-02**: Old import paths keep working for one minor release with a `DeprecationWarning` naming the new path; private names used downstream get public replacements [D-61]
- **PUB-03**: The import audit run on every registered project reports no internal names that a public replacement now covers, other than single-project ones listed in the migration guide [D-61]
- **PUB-04**: Public calibration API (ai-visibility, its OWNER D-66): `CalibrationPair(cluster, human, judge)` at any unit (claim, citation, answer) with a cluster key, `cluster_kappa`, `kappa_from`, and a confusion helper (tp/fn/fp/tn, agreement, strictness fn/(tp+fn), leniency fp/(fp+tn), `owner_constant` when human labels are all one value) [D-65]

## Goal

Every EvalKit name that two or more downstream projects import gets a public, semver-covered
home in 0.3.0; ai-visibility gets the calibration API it asked for; every old import path keeps
working; and each project is told, name by name, what stays internal.

## Why

On 2026-11-17 five of the seven projects import 75 distinct internal or private EvalKit names
(the canary's `imports_not_public`). Nothing protects them: any 0.x release may rename them.
D-61 rule 2 decides which ones become public (used by two or more projects) and which stay
internal (used by one, and that project is told), so the public API grows with real shared use
and not with one project's convenience.

## Rule applied

1. **Two projects** (D-61 rule 2): a name imported by two or more projects is promoted.
2. **Part of the contract**: a type or constant that a promoted name hands to its caller is
   promoted with it, or the promoted name could not be used through public names alone:
   `FaithfulnessResult` (what `faithfulness` returns) and `JudgedClaim` (its `claims` field);
   `AgreementTable` (what `agreement_table` returns); `EXTRACTOR_VERSION` beside
   `JUDGE_PROMPT_HASH` (the two identities stamped into every public `Record`, as
   `extractor_version` and `judge_prompt_hash`: a reader checking one must check the other).
   Arguments a caller builds from machinery (`faithfulness(executor=...)`) are not data and
   follow rule 1 only (see Out of scope).
3. **Where the name lives**: a module whose shared names are all its own is promoted in place
   (old path = new path, nothing to change downstream). A name defined inside `evalkit.pipeline`
   gets a small public module that re-exports the same object; the pipeline path stays silent
   because EvalKit's own code defines and uses the name there (moving it would edit B5.12's
   files and break monkeypatching of module globals). Only names whose implementation moves
   (the calibration functions, PUB-04) warn on the old path.

## Interface

### Promoted names (since 0.3.0)

| Old import path | Public home | Projects | How |
|---|---|---|---|
| `evalkit.cost.Price` | `evalkit.cost.Price` | agentic-analytics, ai-visibility, course-assistant | in place |
| `evalkit.cost.call_cost` | `evalkit.cost.call_cost` | agentic-analytics, ai-visibility, course-assistant | in place |
| `evalkit.cost.load_prices` | `evalkit.cost.load_prices` | agentic-analytics, ai-visibility, course-assistant | in place |
| `evalkit.cost.estimate_tokens` | `evalkit.cost.estimate_tokens` | ai-visibility, course-assistant | in place |
| `evalkit.retry.with_retries` | `evalkit.retry.with_retries` | agentic-analytics, course-assistant | in place |
| `evalkit.stats.Resamples` | `evalkit.stats.Resamples` | agentic-analytics, course-assistant | in place |
| `evalkit.stats.interval` | `evalkit.stats.interval` | agentic-analytics, course-assistant | in place |
| `evalkit.stats.paired` | `evalkit.stats.paired` | agentic-analytics, course-assistant | in place |
| `evalkit.stats.holm` | `evalkit.stats.holm` | agentic-analytics, course-assistant | in place |
| `evalkit.stats.percentile_ms` | `evalkit.stats.percentile_ms` | agentic-analytics, course-assistant | in place |
| `evalkit.scorers.faithfulness.faithfulness` | `evalkit.scorers.faithfulness.faithfulness` | ai-visibility, fraud-review-bedrock | in place |
| `evalkit.scorers.faithfulness.FaithfulnessItem` | `evalkit.scorers.faithfulness.FaithfulnessItem` | ai-visibility, fraud-review-bedrock | in place |
| `evalkit.scorers.faithfulness.FaithfulnessResult` | `evalkit.scorers.faithfulness.FaithfulnessResult` | ai-visibility (rule 2) | in place |
| `evalkit.scorers.faithfulness.JudgedClaim` | `evalkit.scorers.faithfulness.JudgedClaim` | ai-visibility (rule 2) | in place |
| `evalkit.pipeline.answer.SchemaError` | `evalkit.errors.SchemaError` | course-assistant, gemma-grounded-health | alias, old path silent |
| `evalkit.pipeline.judge.JUDGE_PROMPT_HASH` | `evalkit.identity.JUDGE_PROMPT_HASH` | course-assistant, gemma-grounded-health | alias, old path silent |
| `evalkit.pipeline.extract.EXTRACTOR_VERSION` | `evalkit.identity.EXTRACTOR_VERSION` | gemma-grounded-health (rule 2) | alias, old path silent |
| `evalkit.pipeline.calibrate._Pair` | `evalkit.calibration.CalibrationPair` | ai-visibility, course-assistant | moved, old path warns |
| `evalkit.pipeline.calibrate.cluster_kappa` | `evalkit.calibration.cluster_kappa` | ai-visibility, course-assistant | moved, old path warns |
| `evalkit.pipeline.calibrate.kappa_from` | `evalkit.calibration.kappa_from` | ai-visibility, course-assistant | moved, old path warns |
| (new, PUB-04) | `evalkit.calibration.agreement_table`, `evalkit.calibration.AgreementTable` | ai-visibility | new |

`evalkit.scorers.faithfulness` is public as a module, not re-exported from `evalkit.scorers`:
the submodule and the function share the name `faithfulness`, so a re-export would make
`evalkit.scorers.faithfulness` mean the function for some imports and the module for others,
and `import evalkit.scorers` would load the whole judge pipeline. The paths both projects
already use are the public ones.

Each promoted module lists exactly its public names in `__all__`; its other names stay internal.

### PUB-04 calibration API (`evalkit.calibration`)

```python
@dataclass(frozen=True)
class CalibrationPair:          # one labelled unit: a claim, a citation or an answer
    cluster: str                # what the bootstrap resamples (e.g. the answer the unit is in)
    human: bool                 # the owner's label (supported?)
    judge: bool                 # the judge's verdict

def kappa_from(tp: int, fn: int, fp: int, tn: int) -> float            # unchanged
def cluster_kappa(pairs, *, seed: int, n_resamples: int = 10_000,
                  alpha: float = 0.05) -> Interval                      # unchanged maths
def agreement_table(pairs) -> AgreementTable

@dataclass(frozen=True)
class AgreementTable:
    tp: int; fn: int; fp: int; tn: int
    n: int; clusters: int
    agreement: float | None     # (tp + tn) / n
    strictness: float | None    # fn / (tp + fn): judge refuses what the human supports
    leniency: float | None      # fp / (fp + tn): judge supports what the human refuses
    kappa: float | None         # kappa_from(tp, fn, fp, tn)
    owner_constant: bool        # every human label is the same value: kappa is uninformative
```

`pairs` may hold `CalibrationPair`s or the legacy `_Pair(answer, human, judge)` (its `answer`
is its cluster). A ratio whose denominator is 0 is `None`, never 0; an empty list gives `n=0`,
every ratio and `kappa` `None`, `owner_constant=False`. `cluster_kappa` returns exactly what
0.2 returned for the same pairs and seed (golden test). `CalibrationPair` takes its fields in
the same order as `_Pair`, so positional callers (course-assistant) only change the class name.

### Deprecation rule (PUB-02)

- A moved name stays importable from its old path until 0.4.0 through a module `__getattr__`
  that returns the same object and raises `DeprecationWarning`:
  `"evalkit.pipeline.calibrate.<name> is deprecated and will be removed in 0.4.0; use
  evalkit.calibration.<new name>"`. `_Pair` keeps its fields `answer`, `human`, `judge`
  (citewatch's contract test pins them).
- Aliased names keep their pipeline path, silently: it is where EvalKit defines them.
- EvalKit's own code never imports a deprecated path (a test imports every module with that
  warning as an error).
- The audit (`scripts/downstream_imports.py`) reports a promoted old path as
  `public-elsewhere -> <public home>` (from `check_public_api.PROMOTED`), so each project's
  own build sees exactly which imports to change.
- No run output changes: summaries, reports, records and cache keys are byte-identical.

### Private names seen downstream

| Name | Decision |
|---|---|
| `evalkit.pipeline.calibrate._Pair` | public replacement `evalkit.calibration.CalibrationPair`; old path warns |
| `evalkit.pipeline.calibrate._allocate` | stays internal and unchanged: course-assistant only; the calibration sample and sheet are B5.9's to make public (course-assistant D-53) |
| `evalkit.pipeline.calibrate._opaque` | stays internal and unchanged: course-assistant only; blind sheet keys are B5.9's |
| `evalkit.pipeline.answer._extract_json` | no project imports it from EvalKit; stays internal (`parse_answer` is its user) |
| `_atomic_write` | no project imports it from EvalKit (assay-pipeline, course-assistant and fraud-review-bedrock define their own); EvalKit's copies in `run.py` and `datasets/pubmedqa.py` stay internal |
| `evalkit.stats._quantile` | no project imports it (it appears only as a fixture in the audit's self-test); stays internal, its interval maths is public through `interval` and `paired` |

### Spend guard (`evalkit.demo.guard`)

Stays internal. `Guard`, `GuardRefused`, `SqliteSpend` and `PostgresSpend` appear in one project
only (agentic-analytics), and only as strings in its contract test's `USED` table: it imports
none of them, so the audit cannot see them. EvalKit's demo is the other user, and EvalKit's own
modules are not downstream projects under D-61. agentic-analytics should drop the row or keep
it as a canary-only check.

### Single-project leftovers (migration guide)

Still internal after 0.3.0; each project's own build decides (D-61 rule 4). The test compares
this list with the audit re-run over the recorded canary imports.

| Import as recorded | Project | Recommendation |
|---|---|---|
| `evalkit.cost.UnknownPrice` | agentic-analytics | catch `KeyError` (its base class) |
| `evalkit.cost.price_for` | agentic-analytics | `load_prices()[model_id]` (a `KeyError` for an unknown model); keep the import for `free_if_unknown` |
| `evalkit.retry.is_retryable` | agentic-analytics | stays internal; wrap calls in public `with_retries` |
| `evalkit.stats.interval_from` | agentic-analytics | stays internal; candidate when a second project bootstraps outside `Resamples` |
| `evalkit.cost.CostEstimate` | ai-visibility | stays internal; build estimates from public `call_cost` and `estimate_tokens` |
| `evalkit.cost.CostLine` | ai-visibility | stays internal (as `CostEstimate`) |
| `evalkit.cost.TokenAssumptions` | ai-visibility | stays internal (as `CostEstimate`) |
| `evalkit.pipeline.extract.EXTRACTOR_PROMPT` | ai-visibility | stays internal; pin `evalkit.identity.EXTRACTOR_VERSION` instead of the text |
| `evalkit.pipeline.generate.adapter_key` | ai-visibility | stays internal |
| `evalkit.pipeline.generate.provider_for` | ai-visibility | stays internal |
| `evalkit.pipeline.judge.JUDGE_RUBRIC` | ai-visibility | stays internal; pin `evalkit.identity.JUDGE_PROMPT_HASH` instead of the text |
| `evalkit.pipeline.judge.SENTENCE_FORMAT` | ai-visibility | stays internal |
| `evalkit.runtime.Executor` | ai-visibility | stays internal; `faithfulness(executor=...)` needs one (Out of scope) |
| `evalkit.runtime.Store` | ai-visibility | stays internal (as `Executor`) |
| `evalkit.cli` | course-assistant | stays internal; the command line itself is public |
| `evalkit.config.load_task_config` | course-assistant | stays internal; or `TaskConfig.model_validate` on the parsed YAML |
| `evalkit.datasets.load_jsonl_cases` | course-assistant | stays internal; or `Case.model_validate_json` per line |
| `evalkit.pipeline.aggregate.slice_value` | course-assistant | stays internal |
| `evalkit.pipeline.aggregate.summary_json` | course-assistant | stays internal; or `RunSummary.model_dump(mode="json")` |
| `evalkit.pipeline.calibrate.KAPPA_DROP_TOLERANCE` | course-assistant | stays internal (B5.9) |
| `evalkit.pipeline.calibrate.LabelEntry` | course-assistant | stays internal (B5.9) |
| `evalkit.pipeline.calibrate.LabelFile` | course-assistant | stays internal (B5.9) |
| `evalkit.pipeline.calibrate.MIN_JUDGE_COVERAGE` | course-assistant | stays internal (B5.9) |
| `evalkit.pipeline.calibrate.SampledAnswer` | course-assistant | stays internal (B5.9) |
| `evalkit.pipeline.calibrate._allocate` | course-assistant | stays internal and unchanged (B5.9) |
| `evalkit.pipeline.calibrate._opaque` | course-assistant | stays internal and unchanged (B5.9) |
| `evalkit.pipeline.calibrate.build_sheet` | course-assistant | stays internal (B5.9) |
| `evalkit.pipeline.calibrate.heldout_digest` | course-assistant | stays internal (B5.9, B5.14) |
| `evalkit.pipeline.calibrate.import_labels` | course-assistant | stays internal (B5.9) |
| `evalkit.pipeline.calibrate.kappa_regressions` | course-assistant | stays internal (B5.9) |
| `evalkit.pipeline.calibrate.sheet_csv` | course-assistant | stays internal (B5.9) |
| `evalkit.pipeline.generate.is_provider_failure` | course-assistant | stays internal |
| `evalkit.pipeline.judge` | course-assistant | stays internal; read `JUDGE_PROMPT_HASH` from `evalkit.identity` |
| `evalkit.pipeline.judge.redact` | course-assistant | stays internal |
| `evalkit.pipeline.labeller.load_labels` | course-assistant | stays internal (B5.9) |
| `evalkit.pipeline.labeller.save_labels` | course-assistant | stays internal (B5.9) |
| `evalkit.pipeline.task_scorers.TaskScorerError` | course-assistant | stays internal |
| `evalkit.pipeline.task_scorers.resolve_task_scorers` | course-assistant | stays internal |
| `evalkit.pipeline.task_scorers.score_task` | course-assistant | stays internal |
| `evalkit.run` | course-assistant | stays internal; the command line and its exit codes are public |
| `evalkit.run.RunError` | course-assistant | stays internal; exit code 2 is its public form |
| `import evalkit.onboarding` | course-assistant | stays internal |
| `evalkit.adapters.openai.OpenAICompatibleAdapter` | gemma-grounded-health | stays internal; or `evalkit.adapters.get_adapter` by name |
| `evalkit.config.ConfigError` | gemma-grounded-health | stays internal |
| `evalkit.config.load_run_config` | gemma-grounded-health | stays internal; or `RunConfig.model_validate` on the parsed YAML |
| `evalkit.pipeline.answer.answer_schema` | gemma-grounded-health | stays internal |
| `evalkit.pipeline.answer.parse_answer` | gemma-grounded-health | stays internal; catch public `evalkit.errors.SchemaError` |
| `evalkit.run.load_cases` | gemma-grounded-health | stays internal |
| `evalkit.run.load_task` | gemma-grounded-health | stays internal |
| `evalkit.run.read_cases` | gemma-grounded-health | stays internal; or `Case.model_validate_json` per line of `cases.jsonl` |
| `evalkit.run.read_records` | gemma-grounded-health | stays internal; or `Record.model_validate_json` per line of `records.jsonl` |
| `evalkit.tasks.builtin_task` | gemma-grounded-health | stays internal |
| `evalkit.tasks.render_prompt` | gemma-grounded-health | stays internal |
| `evalkit.text.number_sentences` | gemma-grounded-health | stays internal |
| `evalkit.text.split_sentences` | gemma-grounded-health | stays internal |

Already public at another path (fix the import, nothing else): course-assistant's
`evalkit.adapters.base.SystemAdapter`, `register_system` (`evalkit.adapters`) and
`evalkit.schemas.benchmark.*`, `evalkit.schemas.config.TaskConfig`, `evalkit.schemas.summary.*`
(`evalkit.schemas`); gemma-grounded-health's `evalkit.adapters.base.CompletionRequest`,
`RawCompletion` (`evalkit.adapters`).

Near misses, not promoted under rule 1 (one project per name, though two per module): the
config loaders (`load_run_config` gemma, `load_task_config` course-assistant) and the results
readers (`read_records`, `read_cases`, gemma only). They are candidates the next time a second
project imports the same name.

## Out of scope

- Editing any downstream repository (pins, imports, contract tests): each project's own build,
  from this guide (D-61 rule 4).
- A public runner for `faithfulness`: its `executor` argument is `evalkit.runtime.Executor`,
  which one project builds today; it stays internal until a second project calls
  `faithfulness` live.
- Public calibration sampling, sheets and labels (`build_sheet`, `import_labels`, `LabelFile`):
  B5.9.
- New statistics (Clopper-Pearson) on the public stats API: B5.3.
- Removing the deprecated paths: 0.4.0 (B5.10).
- `changelog.md`, `docs/decisions.md` and `docs/graph/catalog.tsv`: the lead's.
