# Changelog

Format: `[TYPE] [NODE] — summary`. Types: FEAT, FIX, DOCS, HARNESS, RELEASE.

## 2026-11-15
- [FEAT] [D3.1] — see commit 20c2fbc
- [FEAT] [B2.3] — README headline chart and sentences generated from the published 200-case run (re-judged, D-52); report deployed to GitHub Pages by workflow (D-47, D-57)
- [FIX] [C3.4] — first full agent run (300 trials, $3.81): agent analytics answers may carry extra columns (D-55); one simulator view per simulator, not per task; held-out report labels; task an-06 month format. Result: Sonnet 81% vs Sol 82–83% public success, 75% each held-out, no separated gap; agents rarely asked clarifying questions (6 and 16 of 60 ambiguous trials)
- [FEAT] [D3.1] — demo backend: `compare()` answer cards (live single-judge path with per-model stage timeouts, replay), PubMed fetch by PMID, in-memory store for visitor data, guard (passcode, session rate limit, per-request cap) and a Postgres daily spend counter with atomic reservation (D-53, D-54)
- [FIX] [C3.4] — agent history sent as conversation turns so provider caches hit (pilot: Sonnet 66% cached, cost per trial $0.040 → $0.018); estimate uses measured pilot tokens (full run $6.16, was $31.52 on placeholders)
- [FIX] [A2.4] — groundedness now judges each claim against all sentences its answer cited; the extractor's per-claim mapping was a positional guess made without the sentence texts (88% of sampled 'unsupported' claims were supported). The first 200-case headline was withdrawn and the run re-judged (D-52, L-37)

## 2026-11-14
- [FEAT] [C3.4] — agent scorers (sql_match / attack outcome, violations, steps over minimum, recovery, cost per success), two-level bootstrap with per-simulator views, Holm-adjusted separation matrix, held-out gap, `evalkit agent-run` with estimate, total cap, resume and an Agents report; agent prompts made cache-friendly (Anthropic automatic caching) (D-51)
- [FEAT] [C3.3] — simulated user on Haiku 5.5 and Luna: cached structured replies from the hidden intent only, deterministic answer guard, simulator recorded on trajectories; knowledge-based answer guard; live B9 check 26/26 PASS with a stricter audit detector (3 live runs, $0.0040) (D-50)
- [FEAT] [C3.2] — agent sandbox and loop: per-trial read-only DuckDB copy with reset hash check (void trials), locked-down SQL, tool allow-list with argument schemas, one structured-output loop for Claude and GPT agents with step budget, cached turns for free replay (D-49)
- [FEAT] [C3.1] — agent suite: seeded Riverbend Outfitters DuckDB (optional `agents` extra, pinned content hash), 32 public tasks (16 analytics incl. 8 ambiguous with hidden intent, 16 prompt-injection attacks whose canaries sit in `system_settings`, so quoting an injection is safe and only obeying it leaks), 8 held-out tasks kept off the repo with a committed manifest, loader that re-checks every gold answer against its SQL (D-48)
- [FIX] [W0.1] — Windows CI: explicit UTF-8 on every text read/write (enforced by an AST test), UTF-8 console output, platform paths in tests (L-32)
- [FIX] [S2.4] — judge every claim against all evidence (not the extractor's citations); pending claims reported separately; live check re-run: PASS
- [FEAT] [S2.4] — explanation faithfulness scorer reusing the pinned extractor and blind judges (live or batch, cached), fixture, live check script (both judges match the fixture, $0.027)
- [FIX] [B2.4] — never overwrite user files (example output, task templates); doctor and run check SDKs and the keys the config needs; README installs the provider extras
- [FEAT] [B2.4] — onboarding: `evalkit init` (config, .env.example, task template, .gitignore; never overwrites), `evalkit doctor` (Python, SDKs, key names, network, prices age, cache), `evalkit report --example` (bundled example run, no keys or network), caveats in every run/report output; shared outputs written 0644
- [FEAT] [B2.3] — README headline chart and generated sentences (`evalkit.report.readme`), `scripts/publish_results.py`, GitHub Pages workflow (OWNER D-47)
- [FIX] [A2.4] — batch holds use 1.5x measured expected output, not the 4,096-token cap (a 200-case run could never start under $20); judges run concurrently (D-46)
- [FIX] [N1.2] — cost model from the 12-case real run: judge calls per claim (4.5 per answer), measured token assumptions; 200-case estimate $14.13 (D-46)
- [FIX] [B2.2] — run safety (D-45): total cost cap across resumes, judge-set lock per run folder, no live re-pay of open batches, dry run without keys, exit 2 with masked messages for every unexpected error
- [FEAT] [B2.2] — CLI: `evalkit run` (resumable run folders, dry run, cost cap exit 3, judging-pending exit 4, `--live-judges` sharing the batch cache, `--json`), `evalkit calibrate export|label|import|score`, `evalkit report [--publish]`; validation before any call; user YAML tasks with local JSONL datasets
- [FIX] [A2.5] — a pending run whose records carry no per-judge scores now aggregates (found by the CLI tests)
- [FEAT] [B2.1] — run report: one self-contained HTML file (D-42) with an accessible SVG quality-vs-cost scatter (CI whiskers, judge dodge, collision-free labels, log scale), model table with PASS/FAIL badges, Holm-labelled pairwise table, failure explorer, calibration and self-preference panels, research banner and caveat stamps; `charts.vl.json` alongside; golden report
- [FIX] [A2.5] — float totals use `math.fsum` so `summary.json` is identical on Python 3.11 and 3.12 (CI failure)
- [HARNESS] [W0.1] — golden gate also runs on the minimum supported Python (L-27)
- [FEAT] [A2.6] — calibration: stratified 100-answer sample (50 tuning / 50 held-out, 20 blind relabel), blind sheet and CSV, local keyboard labelling page (D-40), cluster-bootstrap kappa per judge and family, extraction error rate, self-consistency, rubric-bound `CalibrationReport`, kappa regression gate, publish guard, `docs/calibrate-your-task.md`
- [FEAT] [A2.5] — aggregate: per-judge headline (correct and fully grounded) over all cases, seeded pure-Python bootstrap CIs, paired comparisons with Holm (D-36), thresholds, error accounting, self-preference, derived stamps, atomic `summary.json`; golden end-to-end run
- [FEAT] [A2.4] — dual-judge batch: blind rubric with `judge_prompt_hash`, per-claim batch bookkeeping (resume never pays twice, expired items resubmitted), budget holds across resume, verdict/evidence/quote consistency, one live retry, cross-family dual mode
- [FIX] [A2.4] — four verifier passes; judge blinding per D-38 (no clinical false positives on PQA-L), collect bound covers downloads, per-judge lock
- [FEAT] [A2.4] — `scripts/planted_check.py`: live B6 check with positive controls; both judges pass
- [DOCS] [D-34] — judges and extractor at medium effort, NFR2 $20 per 200-case run; D-35..D-38 recorded

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
- [FEAT] [A2.4] — Dual judging: rubric v1 with its hash in every cache key, blind prompts with only the cited sentences (audit test), B3 precheck without calls, one batch per judge submitted once with per-claim caching, pending status, batch spend under the cost cap (Executor.reserve/settle), strict verdict parsing with counted exclusions, groundedness, 10 planted claims, single/dual judge selection.
- [FEAT] [S2.3] — `attack_outcome`: blocked / partial / succeeded from what the agent did (canaries in answer or nested tool arguments, disallowed tools); AttackBlocked gains `canaries` (additive).
- [FEAT] [S2.3] — Green.
- [MEASURE] [A2.1] — Real smoke run (20 cases × 4 models): 80/80 succeeded, $0.2418 spent; measured tokens put a 200-case run at $8.41. Spend ledger started at docs/measurements/spend-ledger.jsonl.
