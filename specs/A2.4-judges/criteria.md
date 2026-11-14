# A2.4 acceptance criteria

**JDG-01** The rubric is a module constant; `judge_prompt_hash` is the first 16 hex of its sha256, stamped on every ClaimVerdict and part of every judge cache key, so changing the rubric re-judges every claim (no manual bump needed); a stale hash cannot be passed in.
**JDG-02** Rendered judge prompts contain only the claim text and its cited sentences, with model and provider names redacted under D-38: strong names always, ambiguous names whenever they are written as a model (with a version or a second name); ordinary and clinical text (all PQA-L sentences) is never changed.
**JDG-03** For the Anthropic judge, uncached claims are sent as one batch, submitted once (a resumed run reuses the batch id), collected by custom_id and cached per claim; a second run makes no calls.
**JDG-04** The same holds for the OpenAI judge through its Batch API; while a batch is running `judge_claims` returns `status="pending"` without blocking past `max_wait`.
**JDG-05** `parse_verdict` accepts `{verdict, evidence, quote, rationale}`, requires evidence ⊆ the claim's citations, rejects anything else; parse failures and failed batch items exclude the claim from that judge and are counted in `n_errors`.
**JDG-06** `groundedness` is the share of judged claims marked supported; `fully_grounded` is true only when every claim was judged and supported; claims failing the precheck count as judged unsupported.
**JDG-07** Ten planted claims (true in general but absent from their cited sentences) ship as a fixture; `planted_claim_check` fails unless every planted claim is unsupported.
**JDG-08** `select_judges` returns both judges and `dual` when two judges from different providers are available, the available one and `single` otherwise, and refuses when none is.
