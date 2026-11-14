# N1.2 acceptance criteria

**ADP-01** Anthropic adapter maps a `CompletionRequest` to a Messages call and returns a `Completion` with tokens in/out/cached, latency, snapshot id and request id (fake transport fixture).
**ADP-02** OpenAI adapter does the same and records reasoning tokens separately.
**ADP-03** Batch submit returns a batch id; collect returns None while pending and `BatchResult`s keyed by `custom_id` when done; a resumed collect with the same id works.
**ADP-04** `cost_usd` = input × in-price + cached × cached-price + (output + reasoning) × out-price, per `prices.yaml`; unknown model raises.
**ADP-05** `estimate_run_cost(config, n_cases, token_assumptions)` returns a per-node breakdown; `reconcile(records, provider_total)` flags a gap above 10%.
**ADP-06** Transport, 429 and 5xx retry up to 6 times with exponential backoff and jitter (deterministic under a seeded clock in tests); schema errors and 4xx (except 429) are not retried.
**ADP-07** Concurrency never exceeds the configured per-provider limit; the token bucket delays calls beyond the rate (fake clock test).
**ADP-08** `openai_compatible` sends requests to the configured `base_url`; price defaults to 0 with a warning when the model is absent from `prices.yaml`.
**ADP-09** An adapter registered under the `evalkit.adapters` entry-point group is discovered and selectable by name.
