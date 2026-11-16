# B4.1 — EvalKit v0.2: case-aware systems, task scorers, slices

**Wave 4. Depends on:** B2.5 (v0.1.0 released). **Unblocks:** downstream projects that evaluate a whole system rather than a bare model (first user: course-assistant, its OWNER D-11 and node X1.1). Decision: D-59.

## Goal
Let a downstream project run `evalkit run` on its own system under test, score it with its own task scorers, and read results broken down by case groups, without forking EvalKit and without changing anything for v0.1 users.

## Why
Three gaps found by the first downstream project (course-assistant, a retrieval-backed teaching assistant):
1. Adapters see only a rendered prompt string. A system that retrieves, replays prior turns or routes by case needs the case itself, so it would have to parse the question back out of the prompt.
2. A benchmark run computes a fixed set of metrics. Scorers registered under `evalkit.scorers` never run inside `evalkit run`, so they never reach `summary.json` or the report (docs/plugins.md said so).
3. Results are reported per model only. Gates such as "every B3 case passes" or "held-out split only" need per-group figures with intervals.

## Interface (public, semver)

### 1. System under test (SUT-01, SUT-02)
```python
from evalkit.adapters import SystemAdapter, register_system

@register_system("course_ta")            # or the `evalkit.systems` entry-point group
class CourseTA(SystemAdapter):
    version = "1"                         # in the cache key: bump when answers would change

    async def answer(self, case: Case, request: CompletionRequest) -> RawCompletion: ...
    def estimate_cost(self, case: Case, request: CompletionRequest) -> float: return 0.0
```
- `SystemAdapter` subclasses `ModelAdapter`; its `complete()` refuses (it needs a case) and it has no batch API.
- `request` is the request a model would have received: `model` and `model_id` are the alias, `prompt` is the task's rendered prompt, `json_schema` is the answer schema when structured output is on, `params` is empty.
- `evalkit.yaml` routes a model alias to a registered system: `systems: {course-ta: course_ta}` with `course-ta` listed in `models`. The alias needs no price and no API key; its family is `other`; the pre-run estimate counts it at $0 (the adapter's `estimate_cost` holds budget per call against `--max-cost`) and the adapter's `cost_usd` is what is recorded.
- An unknown system name is a configuration error (exit 2) before any call. A system name may not be a model id that a provider already serves.
- Answers go through a `generate_system` node whose cache key holds the whole case (with `meta`), the system name and its `version`, so a rerun is free and a changed case or version asks again. Failures follow the generation taxonomy (§2.4): transient errors become `error="transport"` records; anything else stops the run.
- `Case.meta` (SUT-02): a free-form, deeply frozen JSON object. JSONL datasets fill it from an optional `meta` object per line (for example `{"rule": "B3", "split": "heldout", "prior_turns": [["hi", "hello"]]}`). Empty `meta` is left out of every serialisation, so v0.1 cases keep their bytes.

### 2. Task scorers (TSC-01)
- `TaskConfig.scorers` already lists names. The benchmark set (`label_correct`, `citation_valid`, `groundedness`, `coverage`, `cost`, `latency`) is computed by the pipeline as before. Every other name is a **task scorer**: it is looked up with `evalkit.scorers.get_scorer` (built-in or plugin) when the run is planned; an unknown name exits 2 before any call.
- After judging, each task scorer is called once per record as `scorer(case, completion, model=..., claims=..., verdicts=..., scores=...)` and must return a `ScoreResult`. A scorer that raises stops the run with exit 2 naming the scorer and case (completed calls stay cached, so the rerun is free). A non-finite value is refused.
- Records carry `task_scores: {name: TaskScore(value, version, details)}`; `ModelSummary.task_scores` holds the mean over all cases with the shared 95% bootstrap interval; the report's model table gets one column per task scorer.

### 3. Slices (SLC-01)
- `TaskConfig.slices: [rule, split]` names case fields: `reference_label`, `source`, or a key of `Case.meta`. Values must be strings, integers or booleans; a case without the field falls in the slice value `(none)`.
- `RunSummary.slices` holds one `SliceSummary` per (field, value, model): `n`, error counts, accuracy, per-judge headline and groundedness (empty while judging is pending), citation validity, coverage, task scores, cost per 100 and latency percentiles, each rate with a 95% bootstrap interval drawn over that slice's cases (seeded, so reruns are identical). The run manifest records the slices so `evalkit report` and calibration re-aggregate the same way.
- The report gains a "Slices" section, only when slices exist.

## Backwards compatibility (BWC-01)
- No v0.1 field is renamed, removed or retyped; `SCHEMA_VERSION` stays `1.0`. New fields (`Case.meta`, `RunConfig.systems`, `TaskConfig.slices`, `Record.task_scores`, `ModelSummary.task_scores`, `RunSummary.slices`, `RunManifest.slices`) are optional and omitted from JSON when empty, so for a v0.1 config and task: run ids, config hashes, cache keys, `cases.jsonl`, `records.jsonl`, `summary.json` and `report.html` are byte-identical to v0.1.0 (the golden run proves it).
- v0.1 `ModelAdapter` subclasses and `@scorer` functions work unchanged. `provider_for` and `family_for` gain an optional `systems` argument.
- Known behaviour change: a task that lists an unknown scorer name used to have it silently ignored; it now exits 2 before any call.
- Version 0.2.0 in `pyproject.toml`, `evalkit.__version__` and `uv.lock`, with a changelog entry. Publishing is an owner step.

## Out of scope
- Thresholds on task scores or slices (downstream gates read `summary.json`).
- Model-calling (async, cached, budgeted) task scorers; a task scorer is a local function.
- Pairwise comparisons per slice or per task score.
- CSV `meta` columns.
- Releasing to PyPI and the downstream pin (EKX-04 in the downstream catalog): owner steps after green.
