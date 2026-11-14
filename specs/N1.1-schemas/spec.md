# N1.1 — Schemas and config

**Wave 1. Depends on:** W0.1. **Unblocks:** N1.2, N1.3, N1.4, A2.2, C3.1.

## Goal
One typed, validated vocabulary for everything EvalKit reads and writes: cases, completions, claims, verdicts, scores, records, summaries, calibration labels, agent trajectories, run config and task config. Every file EvalKit writes round-trips through these models without loss.

## Inputs
- Tech Spec §3 (schemas), §4 (versioning), §5 (judge modes and calibration status), PRD FR3, FR4, FR8, FR9, FR12, FR13, FR18, FR19, US7.

## Outputs
- `evalkit/schemas/` — `benchmark.py` (SCH-01), `summary.py` (SCH-02), `calibration.py` (SCH-03), `config.py` (SCH-04), `agent.py` (SCH-05), `__init__.py` re-exporting the public names and `SCHEMA_VERSION = "1.0"` (SCH-06).
- `evalkit/config.py` — `load_run_config(path)` and `load_task_config(path)` reading YAML, with errors that name the file, the field and the problem.
- `tests/fixtures/results_v1/` — one JSON fixture per top-level model; contract tests round-trip each.

## Design rules
- All models `frozen=True, extra="forbid"`: an unknown field is an error, not silently dropped.
- Labels are task-defined strings (`TaskConfig.labels`), not a hard-coded yes/no/maybe, so user tasks (US10) work; the PubMedQA task sets `["yes", "no", "maybe"]` (D-19).
- Citations are 1-based sentence numbers, matching how sentences are shown to models ("Sentence 4"). Range checks against a case's context happen in the citation scorer (A2.2), not here.
- `Interval` enforces `lo <= point <= hi`. `Threshold.op` is `>=` or `<=`. Costs and token counts are non-negative.
- `schema_version` is required on `Record` and `RunSummary`; a major-version mismatch is rejected with a clear error.

## Out of scope
- Cache keys and canonical JSON (N1.3), prices (N1.2), loaders for datasets (N1.4).

## Constraints
- New runtime dependencies: `pydantic>=2.7`, `pyyaml>=6`. Nothing else.
