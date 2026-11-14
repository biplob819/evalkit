# N1.3 acceptance criteria

**RUN-01** Equal inputs give equal keys regardless of dict order; changing any of name, code_version, inputs, model, params, prompt_hash or seed changes the key.
**RUN-02** Store `get` after `put` returns an equal object; a write interrupted before rename leaves no entry; concurrent puts of the same key are safe.
**RUN-03** Every stored output has a lineage sidecar naming its parents; a two-node graph's lineage links child to parent.
**RUN-04** Fan-out over N items runs at most the configured concurrency at once and fans results back in input order.
**RUN-05** Kill a run midway (simulated exception), rerun: completed nodes are cache hits, only the rest execute; a persisted batch id is reused, not resubmitted.
**RUN-06** 100 concurrent workers produce a JSONL file with exactly 100 well-formed lines; `run.log` records node, key, cache hit, latency, cost, error.
**RUN-07** Changing a node's source without bumping `code_version` fails the check; bumping it passes.
**RUN-08** With `max_cost_usd` set, the executor stops scheduling before actual + in-flight estimate exceeds the cap and reports how much work remains.
