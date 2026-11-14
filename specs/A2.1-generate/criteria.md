# A2.1 acceptance criteria

**GEN-01** `Generate` routes `claude-*` to the Anthropic adapter, `gpt-*` to OpenAI and configured endpoints to the OpenAI-compatible adapter; it fans out over models × cases through the executor with per-provider concurrency, and identical requests hit the cache.
**GEN-02** `Validate` turns valid JSON into `Completion.answer`; invalid JSON, a label outside the task's labels, missing fields or non-integer citations give `error="schema"` with no retry and no hints.
**GEN-03** Every completion carries an error type or none: transport failures after retries become `error="transport"` records and are not cached; refusals become `error="refusal"`; `map_settled` returns every item even when some fail.
**GEN-04** With `structured_output: true` every provider receives the answer schema; with false none does; the flag is part of the cache key and recorded in `Completion.params`.
**GEN-05** `scripts/smoke_run.py --n 20` refuses to run without both API keys, prints the cost estimate first, runs generate + validate on 20 seeded cases for the configured models, and writes measured average tokens and cost per model to `docs/measurements/smoke-<date>.json`, which loads as `TokenAssumptions` input.
