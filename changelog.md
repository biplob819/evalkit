# Changelog

Format: `[TYPE] [NODE] — summary`. Types: FEAT, FIX, DOCS, HARNESS, RELEASE.

## 0.3.0

- [RELEASE] [B5.4] — v0.3.0 release candidate: version 0.3.0 in `pyproject.toml`,
  `evalkit.__version__` and `uv.lock`; downstream migration guide
  [docs/downstream.md](docs/downstream.md) (per project: pin, 0.3.0 requirement line, each
  import to replace with its public name, behaviour changes, capabilities asked for); RC canary
  `docs/measurements/downstream-canary-2026-11-17-rc.json`. Not yet tagged or published (V03-03
  is the owner's step). The full contract is in [docs/versioning.md](docs/versioning.md).
  Spend: $0.

**Public additions** (each module's `__all__` is exactly its contract, `tests/test_public_api.py`)
- [FEAT] [B5.2] — Names two or more projects import are public (D-61 rule 2): `evalkit.cost`
  (`Price`, `load_prices`, `call_cost`, `estimate_tokens`), `evalkit.retry` (`with_retries`),
  `evalkit.stats` (`Resamples`, `interval`, `paired`, `holm`, `percentile_ms`),
  `evalkit.scorers.faithfulness` (`faithfulness`, `FaithfulnessItem`, `FaithfulnessResult`,
  `JudgedClaim`); new homes `evalkit.calibration` (`CalibrationPair`, `cluster_kappa`,
  `kappa_from`, `agreement_table`, `AgreementTable`, any label unit), `evalkit.errors`
  (`SchemaError`) and `evalkit.identity` (`JUDGE_PROMPT_HASH`, `EXTRACTOR_VERSION`).
- [FEAT] [B5.3] — `evalkit gate` and `evalkit.metrics` / `evalkit.gates`: the frozen
  `metrics-1` format (labels, units, supplied intervals, `simulated`, standard efficiency
  names), gates on any metric (`>=`, `<=`, `==`; point, lower or upper bound; `fail`/`warn`
  severity; disparity gaps), exit codes 0/1/2/3/4 with precedence 2 > 3 > 4 > 1 > 0;
  `evalkit.stats.clopper_pearson` and `rate_wording`.
- [FEAT] [B5.6] — `evalkit compare` and `evalkit.compare`: per metric and label, new − old with
  a 95% interval (paired two-level bootstrap on shared cases, case-only, or unpaired and
  labelled), Holm-adjusted flags, settings differences, held-out rows as split aggregates;
  exit codes 0/1/2.
- [FIX] [S2.4] — `evalkit.scorers.faithfulness.faithfulness` extracts claims through its own node (`extract_faithfulness_claims`, D-60) and reads an explanation as an answer to its question by default (`question_words=True`); the benchmark extractor and its cache keys are unchanged.
- [FEAT] [B5.12] — Citation support: task scorer `citation_support`, `evalkit.schemas.CitationVerdict`,
  optional `Record.citation_support`, `ModelSummary`/`SliceSummary` `citation_precision` and
  `aim_citation_rate` (omitted when empty).
- [FEAT] [B5.13] — `evalkit.scorers`: `answer_from_rows`, `unsupported_numbers`,
  `clarification_rates`, `ClarificationRates` (scorer `asked_back`), `Rule`, `RuleSet`,
  `RuleError`, `rule_compliance`, `rule_compliance_scorer`, `render_evidence`.

**Not in 0.3.0**
- [FEAT] [B5.13, B5.16] — `value_check`, `value_check_scorer`, `value_check_shares`,
  `ValuePhrase` and `ValueCheckShares` are withheld from the public API (OWNER D-74):
  `from evalkit.scorers import value_check` fails; the code stays in the internal module
  `evalkit.scorers.evidence` until the strict redesign (B5.16) ships in 0.4.0.
  `scripts/check_public_api.py` fails if `evalkit.scorers` exports them.

**Behaviour changes** (nothing that worked in 0.2 by a named import breaks)
- `summary.json` gains an optional per-model `efficiency` object (EFF-02, D-70); every other
  field keeps its bytes. A 0.2 reader that forbids unknown fields must drop it. Reports gain an
  Efficiency section, and shares of exactly 0 or 1 read "0 of n, below X% at 95%".
  `run.json` gains `stopped_at_cost_cap` only when a run stops at its cap.
- Gate bounds (`bound: upper`/`lower`) are exact one-sided 95% Clopper-Pearson for every k
  (D-70; n=150: k=0 → 0.0198, k=1 → 0.0312); displayed intervals stay two-sided.
- `evalkit compare` claims a change only from at least `MIN_CASES` (10) shared cases and a
  non-zero-width interval.
- `sql_match` mismatch details are deterministic across hash seeds: gold's groups are reported
  first, and `example_gold_row` is always a gold row (in 0.2 it could hold a predicted row).
  Scores are unchanged.
- `RunConfig.systems` (added in 0.2.0) is a field a config spec that lists every `RunConfig`
  field must name (gemma-grounded-health's `test_spec_covers_every_run_config_field`).
- Moved paths warn: `evalkit.pipeline.calibrate._Pair`, `cluster_kappa` and `kappa_from` raise a
  `DeprecationWarning` naming their `evalkit.calibration` home and are removed in 0.4.0.
  `from x import *` is narrower for modules that gained an `__all__` (no registered project
  uses it).

**Also in this package ahead of 0.4.0** (listed under "0.4.0 additions" in docs/versioning.md):
`evalkit.heldout` (B5.14), `evalkit.paraphrase` (B5.8), `evalkit.integrity` and
`evalkit calibrate --criterion` (B5.9), `evalkit.security` (B5.11), and opt-in `sql_match`
`equivalences` (B5.15, strict by default).

## 2026-11-17
- [FEAT] [B5.7] — Ledger merged from its lane (awaiting verifier): `evalkit.ledger` and `evalkit.drift`; `evalkit history|experiment|drift|spend` and `report --history`. Hash-chained run, experiment and spend ledgers; median±MAD alarm bands after 5 runs; a one-look freeze guard on held-out sets via B5.14; PSI matching fraud-review-bedrock bit for bit (warn 0.1, alarm 0.25); calibration drift with D-73 tolerances; trend charts; spend alarm at a fraction of the cap. EvalKit's 22 legacy spend lines are sealed by a $0 chained line. Spend: $0.
- [FEAT] [B5.13] — `clarification_rates(cluster=...)` gives a cluster-bootstrap interval over questions (agentic-analytics OWNER D-100), agreeing with its reference within one lattice step; `evalkit.stats.two_level_interval` is public; strict run-folder detection (a foreign summary.json or run.json is never read as an EvalKit run). Spend: $0.
- [FEAT] [B5.16] — value_check closed-world rules merged: every numeric token (digits anywhere, number words) must be bound by a framed "field is value" statement, or the reason is unverifiable. 0 false claims verified across all generators and fraud-review-bedrock's mutation test; 0.08% of its prose reasons verify. The owner chose to ship it as a structured-facts check with a facts-line pattern (D-77). Spend: $0.
- [FIX] [D-76] — Claims plans take the largest of words ÷ 8, sentence-like units, key=value fields, distinct numbers, characters ÷ 50 and 4.5 (10th-percentile ratios from the report); report under-planned share 18.6% → 4.2%; fraud-review-shaped fixture plans 106 for 96; validated overrides (exit 2). Spend: $0.
- [FIX] [N1.2, S2.4] — Cost plans count claims from the texts when they exist (D-76; reported by fraud-review-bedrock, whose quotes were about half the real cost): `evalkit.cost.ClaimsAssumption`, `claims_from_texts`, `measured_claims`, `evalkit.scorers.faithfulness.estimate_faithfulness_cost`, `evalkit run --claims-per-answer`. Every estimate states its claims assumption. On a fixture of 8 × 12 claims the plan went from 36 to 85 (true 96). Spend: $0.
- [FIX] [B5.3, B5.6] — `evalkit gate --out` and `evalkit compare --out/--md` accept a folder (writing gate.json, compare.json or compare.md inside it); the help said "default ./", which read as a folder (reported by assay-pipeline). Spend: $0.
- [FEAT] [B5.16] — Strict value_check merged from its lane (D-74, D-75): verified only when a whole field name or alias is bound to its number by a recognised pattern; phrases bind nothing when another field is named in their clause; hedges and unbound true/false make a reason unverifiable; integer fields exact. 0 false claims verified on every generator and on fraud-review-bedrock's mutation test; on its recorded reasons 10.5% verified, 3.7% contradicted, 85.8% unverifiable. Still withheld from the public API until its verifier passes. Spend: $0.
- [DOCS] — Case study: v0.3.0 shipped (release record cited). Spend: $0.
- [RELEASE] [B5.4] — v0.3.0 released (owner-approved, D-75): tag v0.3.0 at 4ddfef5, evalkit-harness 0.3.0 on PyPI by trusted publishing, install check green on Linux, macOS and Windows; record docs/measurements/release-v0.3.0-2026-11-17.json. Canary workflow now clones projects with full history. Spend: $0.
- [FIX] [CI] — Four linear-time guards in tests/scorers/test_agent_scorers.py used a 0.5 s absolute budget that slow macOS and Windows runners exceeded (0.5–0.8 s); CI was red on four pushes (a21e867, 28028fa, 118b661, 7e58135) with only these flakes plus one fixed record test. Budget raised to 3 s (a quadratic blow-up on these inputs is far slower). Spend: $0.
- [RELEASE] [B5.4] — v0.3.0 release candidate merged from its lane: version 0.3.0, consolidated 0.3.0 changelog, value_check withheld from the public API (D-74, guarded by `WITHHELD`), docs/downstream.md migration guide for all seven projects, RC canary (docs/measurements/downstream-canary-2026-11-17-rc.json: three pass, two known drift, fraud-review-bedrock's bridge test still imports the withheld `ValuePhrase`). Modules of v0.4.0 nodes already green on main ship in 0.3.0 marked provisional. Spend: $0.
- [FIX] [B5.15] — Verifier pass 3 (no must-fix): empty_check refuses a total anywhere in a column name (`sales_total`, `overall_total_sales`, fullwidth letters), and a pivot refuses a run-together `grandtotal`; documented that a month at a quarter start reads as that quarter. Spend: $0.
- [FEAT] [B5.15] — Text period labels for agentic-analytics (its OWNER D-89): "2023 Q1", "Q1 2023", "2023-Q1", bare "Q1" (only when the question names exactly one year) and "2023-03", "March 2023", "Mar 2023"; a quarter label never becomes a month; fiscal questions never convert. empty_check refuses a leading period/total word (`total_sales`), keeps `sales_in_requested_year`. Spend: $0.
- [DECISION] — D-74 (owner): `value_check` leaves v0.3.0 after seven verifier passes; new node B5.16 redesigns it strict for v0.4.0 (verified only when a whole field name or alias is bound to its number). SEV-01 moves to B5.16. Spend: $0.
- [FEAT] [B5.11] — Green after four passes of EvalKit's independent verification (built by assay-pipeline in its lane, D-70/D-72): agent security eval kit with OWASP categories, pass^k with exact bounds, one stopping layer per trial, a scripted attacker that makes no model call, and held-out cases shown only as all-or-nothing aggregates. Spend: $0.
- [FIX] [B5.13] — value_check never verifies what it is unsure of: unchecked times, windows and constants, clauses naming several fields, single shared words, comparison words away from their number and trailing negations are now unverifiable (an independent verifier's generator had 2,616 of 10,080 false claims verified; now 0). On fraud-review-bedrock's 10,225 reasons: 71.4% verified, 5.1% contradicted, 23.5% unverifiable. Spend: $0.
- [FEAT] [B5.11] — Agent security eval kit (lane build on `lane/b5.11`, D-70): `evalkit.security` — attack-case schema with OWASP category, canaries, expected stopping layers and split; held-out cases verified against a B5.14 manifest before any call; k trials and pass^k with exact intervals; a canary in an answer, tool argument or exposed text is a `leak`; configurable stopping-layer attribution; a model-free scripted attacker; split-level summary, metrics-1 for `evalkit gate`, report and compare; `python -m evalkit.security check|run|scripted|report`. Fix round (verifier 4): one-sided bounds agree with the wording, pooled `split: all` rows, no held-out text through exception chains, CLI factory errors exit 2, side effects, suite pinning, `--layers` files, per-case k, several systems in one metrics file, two-level bootstrap blocked rate and `outside_public`. Round 3 (D-72): refused disallowed tool calls count as not blocked in pass^k; held-out category rows opt-in at 5 cases per cell; PRIVACY.md line. Round 4: held-out validation errors name no held-out key, the category minimum counts complete cases, in-memory held-out cases verified against the manifest, canary-match limits documented. Round 5: reported layers withheld on held-out cases, a 5-case minimum per statistic in category rows, de-duplicated trial lines, shared ids not printed. Round 6: held-out category statistics all or nothing (no differencing), fixed error categories for hostile systems. Lifted from assay-pipeline (MIT); its four cases are tests. Spend: $0.
- [FEAT] [B5.15] — Green after two verifier passes: opt-in sql_match equivalences that never turn a wrong answer into a match on any probe; stricter than agentic-analytics' reference. Spend: $0.
- [FIX] [B5.15] — sql_match equivalences made stricter after verification (aligned with agentic-analytics' reference fixes, stricter wherever they differ): whole-word percent names (never percentile), negated-percent questions, month/quarter columns by name and the question's year, count-only empty checks, label-like pivot extras, per-column percent tolerance. Spend: $0.
- [FEAT] [B5.6] — Green after two verifier passes: `evalkit compare` claims a change only with ≥ 10 shared cases, pairs only when most cases are shared, flags every-case-moved-alike regressions, refuses contradictory sidecars, and shows held-out splits only as aggregates. Spend: $0.
- [FEAT] [B5.13] — value_check three outcomes (D-72): verified, contradicted, unverifiable per reason; `value_check_shares` with exact intervals. A reason with nothing checkable now scores 0.0 (unverifiable), not 1.0. On fraud-review-bedrock's reasons: 83.5% verified, 5.6% contradicted, 10.9% unverifiable of checkable reasons; 28 of 30 sampled contradictions are genuine. Spend: $0.
- [FIX] [B5.14] — the exposure scan also reads held-out text held in mapping keys (found by the B5.11 verifier); EvalKit's own held-out agent tasks still scan clean (536 files). Spend: $0.
- [FEAT] [B5.15] — `sql_match` shape equivalences (lane build, merged): opt-in `percent_scale`, `pivot`, `empty_check`, `period_number` under the reference's narrower rules, strict first, `correct_strict` recorded; default output byte-identical. Fix: sql_match failure details no longer depend on PYTHONHASHSEED, and a predicted row is no longer labelled `example_gold_row`. Spend: $0.
- [RELEASE] [B4.1] — v0.2.0 release record added (it was published on 2026-11-16 by trusted publishing from tag v0.2.0, commit 43df77e; files and sha256 from PyPI): docs/measurements/release-v0.2.0-2026-11-16.json. Spend: $0.
- [DOCS] — Portfolio case study (docs/case-study.md): every number cited to a committed file; for the portfolio session's fact-check and the owner's approval before it is published. Spend: $0.
- [FEAT] [B5.9] — Green after three verifier passes: calibration sheets for any criterion, blind repeats and planted items, certification that EvalKit's APIs cannot forge, bar floors. Spend: $0.
- [HARNESS] [B5.13, B5.11] — D-72: value_check gains an "unverifiable" outcome; B5.11 held-out category rows opt-in with a minimum of 5, partial counts as not blocked, PRIVACY.md line on running the user's system. Spend: $0.
- [FEAT] [B5.6] — Run-vs-run compare (lane build, merged): `evalkit compare OLD NEW` and `evalkit.compare` — paired two-level bootstrap on shared cases, case-only resampling, unpaired fallback labelled, Holm-adjusted flags, settings differences listed, baseline rules, held-out aggregates only; tested on assay-pipeline and respectful-offers fixtures. Spend: $0.
- [FIX] [B5.9] — Forgery paths closed (verifier pass 2): EvalKit's public log API refuses calibration lines, certification re-applies the cross-rubric rule, the two scoring lines are one chained write, bar floors (`BAR_FLOORS`) enforced at load and at certification. Spend: $0.
- [FIX] [B5.13] — value_check false negatives on fraud-review-bedrock's real model reasons: pass rate 65.1% → 83.5% with its options, no new false passes (threshold claims on the named field, times of day, policy constants, negated `holds` phrases, the scorer's reason overwrite). Spend: $0.
- [FEAT] [B5.3] — Green after two verifier passes: `evalkit gate` with one-sided 95% Clopper-Pearson bounds (D-70), summary.json `efficiency` per model, held-out numeric splits never printed, typed disparity groups, "0 of n, below X%" wording throughout. Spend: $0.
- [HARNESS] [B5.15] — D-71: opt-in `sql_match` shape equivalences for agentic-analytics (v0.4.0). Spend: $0.
- [HARNESS] [B5.3, B5.11] — D-70: summary.json gains an optional `efficiency` field; gate bounds use one-sided 95% Clopper-Pearson; B5.11 spec written and assigned to assay-pipeline's session on `lane/b5.11`, depending on C3.4 and B5.14. Spend: $0.
- [FEAT] [B5.9] — Calibration integrity sheet (lane build, merged): `evalkit.integrity` — sheets for any criterion (course-assistant's V/C integrity labels, labeller-only context, per-judge kappa on held-out), blind repeats and planted items with labeller self-agreement, certification only through B5.14's logged line; `evalkit calibrate ... --criterion`. The groundedness sheet is byte-identical. Spend: $0.
- [FEAT] [B5.8, B5.13] — Green. B5.8: errors can no longer flag contamination; coverage and run settings reported; provisional until the owner's spot-check. B5.13: after five verifier passes, value checks are fail-safe on negation and never raise on record contents. Spend: $0.
- [FEAT] [B5.3] — Gates on any metric (lane build, merged): `evalkit.metrics` (metrics-1), `evalkit.gates`, `evalkit gate` (exit precedence 2 > 3 > 4 > 1 > 0, `gate.json` on every path, input-classes table), exact Clopper-Pearson (`clopper_pearson`, n=150: k=0 → 0.0198, k=1 → 0.0366), disparity gates, fail/warn severity, held-out aggregates only, the efficiency block in metrics-1 and the report. Tested on assay-pipeline's and respectful-offers' real exports. Spend: $0.
- [FEAT] [B5.14] — Green after two verifier passes: score-once enforced on the log itself, exact tracked path, shallow clones and every error fail closed, a write lock (0 double scorings in 30 races), scan views for HTML entities, hyphenation, NFKC, binary and staged files, and a misspelt `--field` is an error rather than a silent pass. Spend: $0.
- [FEAT] [B5.8] — Paraphrase contamination probe (lane build, merged): gemma's DAT-06 set vendored with provenance (kept sha256 8300d30a…, rejects 0279ecca…), built-in tasks `pubmedqa-paraphrase` and `pubmedqa-paraphrase-original`, rebuild tooling (estimate first, cost cap), and `evalkit report --paraphrase ORIG PARA` with a paired bootstrap per model; provisional until the owner's spot-check. Spend: $0.
- [FEAT] [B5.2] — Green after verification (all 86 recorded downstream imports still work; moved calibration paths warn once until 0.4.0); verifier fixes: iterator inputs, `dir()`, star-import note. Spend: $0.
- [HARNESS] [B5.8] — D-68: the paraphrase probe adopts gemma-grounded-health's set built by EvalKit's DAT-06 method; lane opened. Spend: $0.
- [FEAT] [B5.13] — Agent and decision scorers (lane build, merged): `answer_from_rows`, `clarification_rates`/`asked_back`, `rule_compliance` (AST allow-list predicates, never eval; configurable cited key and allow predicate) and `render_evidence`/`value_check`, each tested on its asking project's cases (agentic-analytics, fraud-review-bedrock). Spend: $0.
- [FEAT] [B5.2] — Public API 0.3 (lane build, merged): cost, retry, stats, faithfulness and calibration names promoted by the two-project rule; `evalkit.calibration` (CalibrationPair, cluster_kappa, kappa_from, agreement_table) for ai-visibility; `evalkit.errors`, `evalkit.identity`; moved calibration paths warn until 0.4.0; 55 single-project names listed with recommendations. Spend: $0.
- [FEAT] [B5.14] — Held-out discipline (lane build, merged): `evalkit.heldout` manifest, score-once guard, exposure scan (EvalKit's own held-out agent tasks scan clean against 444 tracked files) and hash-chained append-only log with `is_certified`. Spend: $0.
- [FEAT] [B5.12] — Green after verification: citation support confirmed; verifier fixes (empty per-judge lists omitted, `calibrate score` keeps an unfinished run pending, unjudged citation claims shown, CIT-03 fixture replies labelled as hand-written). Spend: $0.
- [HARNESS] [B5.3, B5.7, B5.5] — D-67: a standard efficiency block for every project and model (tokens, cache hits, cost per case and per correct, latency p50/p95/p99, retries, eval overhead, mode, price date) with intervals (EFF-01, EFF-02), variation across runs (EFF-03) and an index view (EFF-04); gemma pins B5.12 once verified; respectful-offers builds its exporter now. Spend: $0.
- [FEAT] [B5.12] — Per-sentence citation support (gemma-grounded-health, D-65): a task listing `citation_support` gets a citation-judge pass over cached claims (supports and role per cited sentence, `CITATION_PROMPT_HASH` be34ebe577bc96ae), citation precision and aim-citation rate per model and judge with bootstrap CIs in records, summary, slices and report. The claim judge's prompt hash (9a3a8246049f6f85), cache keys and every pre-existing run output are unchanged. Spend: $0.
- [HARNESS] [B5.2, B5.13, B5.14] — D-66: parallel lanes, one builder per node in its own worktree, lead merges; dependencies rewired to real needs (B5.13, B5.14, B5.8, B5.9). B5.2, B5.13 and B5.14 started beside B5.12. Spend: $0.
- [HARNESS] [B5.1] — Green after three verifier passes: the canary never hides a real failure behind a known one, runs in an allowlisted environment with a private HOME per project, and records `pass`, `known-drift`, `fail`, `setup-error`, `nothing-ran` or `no-tests`. Baseline on fresh clones: agentic-analytics 109, ai-visibility 18, assay-pipeline 60, course-assistant 151, fraud-review-bedrock 965 tests pass; gemma-grounded-health known drift (1). Spend: $0.
- [HARNESS] [B5.12–B5.14] — D-65: per-sentence citation support (B5.12, next, for gemma-grounded-health), agent and decision scorers (B5.13, v0.3.0: answer-from-rows, clarification, rule compliance, structured-evidence faithfulness), held-out discipline and tamper-evident logs (B5.14, v0.4.0); public calibration API (PUB-04), release-gate rigour and latency/cost gates (GAT-09, GAT-10), silent-skip-free baselines (RVR-03), trend chart and spend ledger (LED-05, LED-06). Spend: $0.
- [HARNESS] [B5.3, B5.11] — D-64 (assay-pipeline): exact Clopper-Pearson bounds (GAT-07) and fail/warn gate severity (GAT-08) in B5.3; new node B5.11 agent security eval kit for v0.4.0. Spend: $0.
- [HARNESS] [B5.3, B5.7] — D-63: respectful-offers adopts EvalKit through Wave 5; B5.3 adds multi-field labels, units, supplied intervals, a `simulated` flag (GAT-05) and group-disparity gates (GAT-06); B5.7 adds a drift and calibration monitor (LED-04). Off-policy coverage stays in respectful-offers. Spend: $0.
- [HARNESS] [B5.3–B5.10] — Wave 5 widened (OWNER D-62): fraud-review-bedrock (Field PM Pack) is the seventh downstream project; B5.3 gates any named metric from a metrics-summary format (assay-pipeline's needs); new B5.6 run-vs-run compare, B5.7 monitoring and improvement ledger, B5.8 paraphrase contamination probe, B5.9 calibration integrity sheet, B5.10 v0.4.0. Spend: $0.
- [HARNESS] [B5.1] — Downstream registry and canary: `docs/downstream.tsv` (six projects, pins confirmed against GitHub), `scripts/downstream_imports.py` (every EvalKit name each project imports, public or internal), `scripts/downstream_canary.py` and the weekly `downstream.yml` workflow (each project's EvalKit tests against this checkout, no keys). Baseline: agentic-analytics, ai-visibility, assay-pipeline and course-assistant pass; gemma-grounded-health fails one test because v0.2 added `RunConfig.systems` and its config test requires every field (fix goes in the B5.4 migration guide). Spend: $0.
- [HARNESS] [B5.1–B5.5] — Wave 5 added (OWNER D-61): one eval loop for the six downstream projects. Registry and canary (B5.1), public API 0.3 for the internals two or more projects use (B5.2), gates on task scores and slices (B5.3), v0.3.0 with a migration guide (B5.4), cross-project eval index (B5.5). B5.1 spec written. Spend: $0.
- [FIX] [S2.4] — faithfulness extraction runs through its own node `extract_faithfulness_claims` (D-60): no citation list for the extractor (long evidence pages no longer overrun the output limit and fail as "schema"; one retry at 4x the limit if a reply is still cut off), and the explanation is read as an answer to its question (`faithfulness(..., question_words=True)`, default), so a fragment may borrow its subject from the question under B11; coverage is still against the explanation. The benchmark extractor (`extract_claims` v2, `ext2-f484fe5e`) and its cache keys are unchanged. Spend: $0.
- [DEMO] [D3.2] — Green: demo live at https://evalkit-demo.streamlit.app/ (replay for everyone; live behind the passcode on the owner's keys or the visitor's own, D-59); deployment checked end to end. Spend: $0.1312 (one live request).

## 2026-11-15
- [RELEASE] [B4.1] — v0.2.0 prepared (not yet tagged or published): systems under test (`SystemAdapter.answer(case, request)` gets the whole `Case`; `systems:` in `evalkit.yaml`; `evalkit.systems` entry-point group), `Case.meta` from a JSONL `meta` object, task-declared scorers (every `TaskConfig.scorers` name outside the benchmark set runs inside `evalkit run`; `Record.task_scores`, `ModelSummary.task_scores` with 95% CIs, report columns), per-slice summary and report (`TaskConfig.slices`, `RunSummary.slices`). Backwards compatible: new fields are omitted when empty, so v0.1 run ids, cache keys, results and reports are byte-identical. Breaking changes for v0.1 tasks (D-59): (1) an unknown scorer name now exits 2 instead of being ignored; (2) a scorer name that matches a registered plugin, which v0.1 ignored, now runs, so its records and summary gain `task_scores`; (3) such a plugin must accept keyword context (`model=`, `claims=`, `verdicts=`, `scores=`, or `**ctx`), or the run exits 2. Runs whose tasks name only benchmark scorers are byte-identical
- [RELEASE] [B2.5] — v0.1.0 published: evalkit-harness 0.1.0 on PyPI (wheel + sdist) by trusted publishing from tag v0.1.0; install smoke passed on three OSes; B2.5 green. Release record: docs/measurements/release-v0.1.0-2026-11-15.json. Spend: $0.
- [RELEASE] [B2.5] — v0.1.0 prepared: SciFact as a second built-in task, CSV datasets, quickstart / task / plugin / cost / versioning docs, community files, privacy statement, CI action example, public API contract test, post-release smoke job on three OSes
- [FEAT] [D3.2] — demo page (Streamlit): replay by default with six prepared samples (`evalkit demo-cache`, $0.99 in three runs), live mode on the owner's keys behind the passcode or on a visitor's own keys (D-58), cards that fill in per model, aggregate-only logging; failed or unchecked cards are re-answered by the next cache run
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
