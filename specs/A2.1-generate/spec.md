# A2.1 — Generate and validate

**Wave 2. Depends on:** N1.2, N1.3, N1.4. **Unblocks:** A2.3.

## Goal
Turn sampled cases into validated `Completion`s for every configured model: the Generate node calls the right adapter, the Validate node parses the structured answer, and every failure is recorded with its type instead of stopping the run.

## Outputs
- `evalkit/pipeline/answer.py` — `answer_schema(labels)` (strict JSON schema for `{label, answer, citations}`) and `parse_answer(raw_text, labels)`.
- `evalkit/pipeline/generate.py` — `provider_for(model_id, endpoints)`, `GenerateInput`, `Generate` node (cache-keyed on prompt, model id, effort, structured flag), `Validate` node building `Completion` (GEN-01, GEN-02).
- Error taxonomy (GEN-03): transport failures after retries → `Completion(error="transport")` (not cached, so a rerun retries them); refusals → `error="refusal"`; unparseable or invalid answers → `error="schema"`, never retried (B1).
- `RunConfig` gains per-model effort (`efforts`, `default_effort`, `judge_effort`, `extractor_effort`; additive, results format 1.1) (GEN-04 with `structured_output`).
- `Executor.map_settled` returns per-item results or exceptions so one failure never stops a run.
- `scripts/smoke_run.py` and `configs/pubmedqa.yaml` — the 20-case smoke run that writes measured token averages to `docs/measurements/` (GEN-05).

## Rules
- Structured output is on for every provider or off for every provider; the choice is in the cache key and in `Completion.params["structured_output"]`.
- Generation effort defaults to `medium` for every model (comparable settings, D-24); judges and extractor default to `low` (D-28).
- The smoke run needs both API keys; it refuses to start without them and prints the estimate first.
