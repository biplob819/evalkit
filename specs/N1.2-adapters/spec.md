# N1.2 — Model adapters and cost model

**Wave 1. Depends on:** N1.1. **Unblocks:** A2.1, A2.4, C3.2.

## Goal
One `ModelAdapter` interface that every provider implements, so the rest of EvalKit never imports a provider SDK. Every call returns a `Completion` with tokens, latency and cost, and every run knows its cost before it starts.

## Outputs
- `evalkit/adapters/base.py` — `ModelAdapter` protocol (`complete`, `submit_batch`, `collect_batch`), `CompletionRequest`, `BatchRequest`, `BatchResult`, registry and entry-point discovery (`evalkit.adapters`, ADP-09).
- `evalkit/adapters/anthropic.py` (ADP-01), `openai.py` (ADP-02), `openai_compatible.py` with `base_url` (ADP-08). Batch support for Anthropic and OpenAI (ADP-03).
- `evalkit/prices.yaml` and `evalkit/cost.py` — per-completion cost including cached and reasoning tokens (ADP-04); `estimate_run_cost()` and `reconcile()` (ADP-05).
- `evalkit/retry.py` — backoff with jitter for transport, 429 and 5xx; schema errors never retried (ADP-06).
- `evalkit/ratelimit.py` — per-provider semaphore plus token bucket (ADP-07).

## Design rules
- Provider SDKs (`anthropic`, `openai`) are optional extras imported lazily inside their adapter; `import evalkit` never needs them.
- Model ids live in config only; adapters never hard-code a model name. Model snapshot id comes from the provider response.
- Keys are read from `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` (or the endpoint's configured env var) at call time and never logged, stored or placed in a `Completion`.
- No network in tests: adapters are tested against recorded fixtures and a fake transport.
- `prices.yaml` records per-million-token prices with a `checked_on` date per model; unknown model → explicit error, never a silent $0 (except `openai_compatible`, which defaults to 0 and says so).
