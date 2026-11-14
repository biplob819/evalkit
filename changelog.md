# Changelog

Format: `[TYPE] [NODE] — summary`. Types: FEAT, FIX, DOCS, HARNESS, RELEASE.

## 2026-11-14

- [HARNESS] [W0.1] — Repo skeleton (`evalkit-harness`, src layout, Python ≥3.11), CI and release workflows, build graph (26 nodes) and capability catalog (144 rows), `verify_graph_eligibility.py`, `node_start.py`, `node_converge.py` (12 gates), `secret_scan.py`, `status.py`, every gate with a self-test, decision log seeded D-1 to D-14.
- [FIX] [W0.1] — Verifier findings: AST-checked proofs under tests/, git-diff errors fail gate 11, check-only node-start writes nothing, self-tests require the defect's identity, wider secret patterns, strict graph and catalog parsers.
- [HARNESS] [W0.1] — Green. HAR-08 (PyPI name) moved to B2.5 (OWNER D-20).
- [FEAT] [N1.1] — `evalkit.schemas` (results format v1: benchmark, summary, calibration, config, agent), `evalkit.config` YAML loaders with file-and-field errors, v1 fixtures and round-trip contract tests.
- [FIX] [N1.1] — Verifier findings: finite floats, required well-formed schema_version, honest RunSummary stamps, deep immutability, strict ints, linked claims and verdicts, loader errors that name the file, duplicate-key YAML rejected.
- [FEAT] [N1.1] — Green.
- [FEAT] [N1.3] — `evalkit.runtime`: typed Node contract, canonical-JSON cache keys, content-addressed store with lineage and batch-id meta, async executor (per-provider concurrency, fan-out in order, resume from cache, spend cap counting in-flight estimates), single-writer JSONL, code-version manifest check.
- [FEAT] [N1.4] — PubMedQA PQA-L loader (pinned checksum, 1,000 rows, cached, `long_answer` dropped), rule-based sentence splitter checked on all 1,000 abstracts, seeded label-stratified sampler, built-in `pubmedqa` task and prompt rendering, `DATA_CARD.md` with the contamination probe spec.
- [FIX] [N1.3] — Verifier findings: money values must be finite and non-negative, budget reserved inside the concurrency slot, identical in-flight work coalesced, batch ids reused via `submit_once`, corrupt cache entries recomputed, version manifest keyed by node name and hashing MRO plus schemas, writer refuses NaN and late writes.
- [FEAT] [N1.3] — Green.
- [FIX] [N1.4] — Verifier findings: splitter recovers 41 wrong merges (initials, units, unclosed brackets, spaced decimals), download verified before an atomic write, network failures raise DatasetError, duplicate ids rejected, data card claims made true or dated, probe spec fully pinned, PubMedQA credited in the README.
- [FEAT] [N1.4] — Green.
- [FEAT] [N1.2] — Adapters: Anthropic (Messages, Message Batches) and OpenAI (Chat Completions, Batch) plus OpenAI-compatible servers, behind one interface with entry-point plugins; no sampling params, explicit effort, structured JSON; `prices.yaml` (Claude 5.5 family, gpt-6.1-sol, gpt-6-luna, gpt-6.1-terra), per-call cost, pre-run estimate and reconciliation; retry policy (6 attempts, full jitter, transport errors only); token-bucket pacing.
- [FIX] [N1.2] — Verifier findings: expired/cancelled batches keep billed results, real SDK error types, damaged batch lines tolerated, duplicate custom_ids refused, explicit effort enforced, token bucket wired into every adapter call, plugin failures isolated, compatible-server URL and key checks, long-context price tiers, judges at low effort (OWNER D-28, estimate $10.35).
- [FEAT] [N1.2] — Green. Wave 1 (N1.1-N1.4) complete.
- [FEAT] [A2.2] — `evalkit.scorers`: Scorer protocol, registry and isolated entry-point plugins; label_correct, citation_valid, cost and latency (errors count as wrong).
- [FIX] [A2.2] — Verifier findings: built-in scorers can never be replaced by plugins, duplicate and mismatched plugin names warn, failed plugins load once, Unicode-normalised labels, strict finite scores, picklable scorers.
- [FEAT] [A2.1] — Generate and Validate nodes: provider routing, per-provider pools, structured answers with a strict parser, error taxonomy (transport not cached, refusal, schema never retried), `Executor.map_settled`, per-model effort in RunConfig, reference `configs/pubmedqa.yaml`, smoke-run module and script.
- [FEAT] [A2.2] — Green.
- [FEAT] [S2.1] — `sql_match`: result-set comparison (column reordering by name, multiset rows, ordered option, numeric tolerance, strict booleans/None/strings) with 21 golden cases.
- [FIX] [A2.1] — Verifier findings: budget exhaustion and configuration errors stop the run (only transient provider errors are 'transport'), answers must be one JSON object (validate v2), .env parsing, smoke exit codes and labels, tighter OpenAI routing.
- [FEAT] [S2.2] — Retrieval metrics: recall@k, MRR, nDCG@k (binary or graded), positions as returned, duplicates earn no credit.
- [FEAT] [A2.1] — Green (real smoke run pending API keys).
- [FEAT] [A2.3] — Claim extractor: pinned prompt with hashed extractor_version, ExtractClaims node at low effort with structured output, B11 checks (no invented citations, no duplicates, no empty extraction), coverage v1 (D-31).
- [FIX] [S2.1] — Verifier blocker fixed: unordered matching honours the tolerance exactly (exact-first, grouped, Hopcroft-Karp matching, windowed candidates), exact big-integer comparison, Decimal as numbers, 20k noisy rows < 3 s, brute-force fuzz.
- [FIX] [S2.2] — Any negative gain raises, all-zero gains undefined, string inputs rejected.
- [FEAT] [S2.1] — Green.
- [FEAT] [S2.2] — Green.
- [FIX] [A2.3] — Verifier findings: extractor v2 (version stamped on run, stale versions rejected), prompts hashed by the version check (manifest rebuilt for the new hash format, no behaviour change to generate/validate), per-item transport errors, colon-free case ids, B11 add/drop guard (D-32), coverage as a ScoreResult with details, aligned ClaimSet.
- [FEAT] [A2.3] — Green.
