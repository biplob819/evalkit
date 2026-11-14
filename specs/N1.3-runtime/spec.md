# N1.3 — DAG runtime

**Wave 1. Depends on:** N1.1. **Unblocks:** A2.1, C3.1.

## Goal
Run an evaluation as a typed graph of nodes where every node's output is cached by content, so resume, exact rescoring and replay are one mechanism (D-1, D-2).

## Outputs
- `evalkit/runtime/node.py` — `Node[In, Out]` with `name`, `code_version`, `run()`; `cache_key()` = sha256 of canonical JSON of (name, code_version, inputs, model, params, prompt_hash, seed) (RUN-01).
- `evalkit/runtime/store.py` — content-addressed store at `.evalkit/store/<k[:2]>/<k>.json`, atomic temp-then-rename writes (RUN-02); lineage sidecar `{key, node, parents, started_at, finished_at, cost_usd}` (RUN-03).
- `evalkit/runtime/executor.py` — asyncio executor with fan-out over items and fan-in, per-provider semaphores from N1.2 (RUN-04); rerunning a graph skips cached nodes; batch ids persisted under their key (RUN-05); running spend cap (RUN-08).
- `evalkit/runtime/writer.py` — single writer task appending JSONL records and `run.log` (RUN-06).
- `scripts/check_code_versions.py` + gate wiring — fails when a node's source hash changes without a `code_version` bump (RUN-07).

## Design rules
- Canonical JSON: sorted keys, no whitespace, floats via `repr`, pydantic models dumped in JSON mode.
- A crashed run leaves no partial store entries (atomic writes) and resumes from the last completed key.
- The spend cap stops scheduling new calls when actual + in-flight estimated spend would pass `max_cost_usd`; completed work stays cached; exit code 3 is raised by the CLI later (B2.4).
