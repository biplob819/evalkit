# N1.1 acceptance criteria

**SCH-01 — benchmark schemas**
- `Case`, `Answer`, `Completion`, `Claim`, `ClaimVerdict`, `Scores`, `Record` exist with the Tech Spec §3 fields.
- Unknown fields are rejected; negative tokens, cost or latency are rejected; citations must be ≥ 1.
- `Completion.error` is one of transport, schema, refusal or None; an `answer` must be None when `error == "schema"`.
- `ClaimVerdict.verdict` is supported or unsupported and carries `evidence` and optional `quote`.

**SCH-02 — summary schemas**
- `Interval` rejects `lo > point` or `point > hi`.
- `ModelSummary` carries `n_errors` by type, headline per judge as `Interval`, `passes_threshold` and `failed_thresholds`.
- `RunSummary` carries `judge_mode` (dual or single), `calibration_status` (calibrated or uncalibrated), `judging_status`, `self_preference`, and both judges' `JudgeAgreement` when `judge_mode == "dual"`.

**SCH-03 — calibration label**
- `CalibrationLabel` has `claim_id`, `split` (tuning, heldout, relabel), `label` (supported, unsupported), `extraction_error`.

**SCH-04 — run and task config**
- `Threshold(metric, op, value)` with op `>=` or `<=`; `RunConfig` holds models, judges, extractor, thresholds, structured_output, max_output_tokens, concurrency, seed, `max_cost_usd`, `endpoints`.
- `load_run_config` and `load_task_config` read YAML; an error names the file and the dotted field path; a missing file says so.
- `TaskConfig.labels` must be non-empty and unique.

**SCH-05 — agent schemas**
- `AgentTask` with `source_project: str`, `GoldOutcome` as a discriminated union of `ResultSet` and `AttackBlocked`, `split` public or heldout.
- `Step`, `Trajectory` (with simulator, seed, cost) and `AgentScore` per Tech Spec §3.

**SCH-06 — results format v1**
- `SCHEMA_VERSION == "1.0"` is exported; `Record` and `RunSummary` reject a different major version.
- A JSON fixture for each of `Record`, `RunSummary`, `CalibrationLabel`, `Trajectory`, `AgentScore`, `RunConfig` round-trips: parse → dump → parse gives an equal object, and dump is stable.
- Every public schema name is importable from `evalkit.schemas`.
