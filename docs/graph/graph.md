# EvalKit build graph

Source of truth for build-node dependencies. Parsed by `scripts/harness_lib.py`.
A node may start only when every node in its `Depends on` cell is green (all its rows in `catalog.tsv` are green).
Edge rules: no cycles; a node inherits the constraints of what it depends on; `Verified by` must pass before a node flips green.
Generated from EvalKit Tech Spec §11.2 on 2026-11-14; edit here from now on and keep the Tech Spec in sync.


## Wave 0 — harness

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **W0.1** | Repo and harness | — | node_converge + verifier | Repo, uv project, CI workflow, the three harness scripts, each gate with `--self-test`, decisions/lessons/changelog files | — |

## Wave 1 — core

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **N1.1** | Schemas and config | W0.1 | node_converge + verifier | `evalkit.schemas` (§3), `RunConfig`/`TaskConfig` loaders, results format v1 fixtures | US1, US6 |
| **N1.2** | Model adapters and cost model | N1.1 | node_converge + verifier | Anthropic and OpenAI adapters (sync and batch), `prices.yaml`, pre-run cost estimate | US1 |
| **N1.3** | DAG runtime | N1.1 | node_converge + verifier | Node contract, content-addressed store, lineage, executor, resume, single writer | US1, US4 |
| **N1.4** | PubMedQA task and data card | N1.1 | node_converge + verifier | PQA-L loader, sentence splitter, stratified sampler, `tasks/pubmedqa.yaml`, `DATA_CARD.md` | US1, US10 |

## Wave 2 — benchmark pipeline

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **A2.1** | Generate and validate | N1.2, N1.3, N1.4 | node_converge + verifier | Generate and Validate nodes, retry and error rules (§2.4), a 20-case smoke run | US1 |
| **A2.2** | Deterministic scorers | N1.1, N1.4 | node_converge + verifier | Label correct, citation validity, cost, latency scorers in `evalkit.scorers` | US1, US6 |
| **A2.3** | Claim extractor | A2.1 | node_converge + verifier | ExtractClaims node, pinned extractor prompt and version | US2 |
| **A2.4** | Dual-judge batch | A2.3, N1.2 | node_converge + verifier | JudgeOpus and JudgeSol batch nodes, rubric, blinding test, planted-claim test | US2, US3 |
| **A2.5** | Aggregate and CIs | A2.2, A2.4 | node_converge + verifier | Aggregate node: bootstrap, paired comparison, thresholds, error accounting, self-preference table, `summary.json` | US1, US4, US7 |
| **A2.6** | Calibration | A2.4, A2.5 | node_converge + verifier | `evalkit calibrate`, tuning and held-out splits, kappa with CIs per family, regression fixture | US3 |

## Wave 2 — reporting and public release

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **B2.1** | Report and dashboard | A2.5, A2.6 | node_converge + verifier | Static dashboard (scatter with CIs, model table, failure explorer, calibration panel) | US2, US3, US7 |
| **B2.2** | CLI | A2.6, B2.1 | node_converge + verifier | `evalkit run`, `calibrate`, `report` with Typer and a cost progress bar | US1 |
| **B2.3** | README chart and dashboard deploy | B2.1 | node_converge + verifier | Headline chart from `summary.json`, README, Cloudflare Pages deploy | US5 |
| **B2.4** | Public CLI and onboarding | B2.2 | node_converge + verifier | `init`, `doctor`, `report --example`, `--dry-run`, `--max-cost`, exit codes, `--json`, single-judge and uncalibrated stamps | US12–US16 |
| **B2.5** | Open-source packaging and v0.1.0 | B2.3, B2.4 | node_converge + verifier | PyPI `evalkit-harness`, Python and OS CI matrix, docs, example tasks, community files, GitHub Action, tag v0.1.0 | US6, US12, US16, US17 |

## Wave 2 — downstream scorer library

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **S2.1** | `sql_match` scorer | A2.2 | node_converge + verifier | Result-set match scorer, its criteria and tests | US6 |
| **S2.2** | Retrieval metrics | A2.2 | node_converge + verifier | Recall@k, MRR, nDCG scorers | US6 |
| **S2.3** | Attack-outcome scorer | A2.2 | node_converge + verifier | Attack blocked or succeeded scorer | US6 |
| **S2.4** | Explanation faithfulness | A2.3, A2.4 | node_converge + verifier | Claim-level faithfulness scorer reusing the extractor and judges | US6 |

## Wave 3 — agent track

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **C3.1** | Agent tasks | N1.1, N1.3 | node_converge + verifier | AgentTask YAML loader, 40 tasks, held-out split of 8 | US11 |
| **C3.2** | Sandbox and trial runner | C3.1, N1.2 | node_converge + verifier | DuckDB sandbox, reset check, tool allow-list, trajectory recording, step budget | US11 |
| **C3.3** | Simulated user | C3.2 | node_converge + verifier | Haiku 5.5 and Luna simulators, seeded, with the B9 checks | US11 |
| **C3.4** | Agent scorers and aggregate | C3.2, C3.3, S2.1, S2.3, A2.5 | node_converge + verifier | Trajectory scorers, pass@k, pass^k, two-level bootstrap, `evalkit agent-run`, dashboard tab | US11 |

## Wave 3 — demo

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **D3.1** | `compare()` and guard | A2.2, A2.4 | node_converge + verifier | `compare()`, PMID fetch, live single-judge path, guard with persistent KV counter | US8, US9 |
| **D3.2** | Demo page and replay | D3.1, B2.2 | node_converge + verifier | `evalkit demo-cache`, Streamlit page in `portfolio-demos`, banner, deploy | US8, US9 |

## Wave 4 — v0.2 (downstream needs)

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **B4.1** | EvalKit v0.2: case-aware systems, task scorers, slices | B2.5 | node_converge + verifier | `SystemAdapter` (gets the whole `Case`, routed by `systems:`), `Case.meta`, task-declared plugin scorers inside `evalkit run`, per-slice summary and report, v0.2.0 | US6 |

## Wave 5 — one eval loop for every downstream project (D-61, D-62)

| Node | Title | Depends on | Verified by | Deliverable | Stories |
| --- | --- | --- | --- | --- | --- |
| **B5.1** | Downstream registry and canary | B4.1 | node_converge + verifier | `docs/downstream.tsv` (the six projects, their pins and offline contract-test commands), `scripts/downstream_imports.py` (every EvalKit name each project imports, public or internal), `downstream.yml` workflow running each project's contract tests against this checkout | US6 |
| **B5.2** | Public API 0.3: promote what downstream uses | B5.1, S2.4 | node_converge + verifier | Internals used by two or more projects (cost, retry, stats, config, results readers, answer parsing, judge and extractor identity, calibration kappa, spend guard) become documented public names; old paths keep working with a `DeprecationWarning`; private names used downstream get public replacements | US6 |
| **B5.3** | Gates on any metric | B5.2 | node_converge + verifier | A metrics-summary format any project can emit (name, value, n, CI, labels such as model, split, category); thresholds on any named metric: counts, rates, booleans, USD, with `>=`, `<=` or `==` on the point value or the 95% CI lower or upper bound, per label; held-out splits reported only as aggregates; `evalkit gate` with exit codes and `--json`; GitHub Action input (D-62) | US6 |
| **B5.4** | v0.3.0 and downstream migration guide | B5.2, B5.3, B5.6, B5.12, B5.13 | node_converge + verifier | Version 0.3.0 (public API 0.3, gates, compare, citation check, agent and decision scorers except value_check, which moves to B5.16 in v0.4.0 per D-74), changelog, `docs/downstream.md` with the exact import replacements per project, owner-tagged release on PyPI | US6 |
| **B5.5** | Cross-project eval index | B5.4, B5.7, B2.3 | node_converge + verifier | A page on the EvalKit site: per project, the headline metric with its CI, calibration stamp, gate status, measured improvements from its ledger, lessons link, EvalKit version and run date, read from committed files at a pinned commit; the portfolio site reads it (D-62) | US5, US6 |
| **B5.6** | Run-vs-run compare | B5.3 | node_converge + verifier | `evalkit compare <old> <new>` and a public function: per metric and label, the change with a 95% interval (paired two-level bootstrap on shared case ids, else unpaired and said so), Holm-adjusted flags, snapshot and code-version differences stated, JSON and a markdown table, held-out aggregates only (D-62) | US6 |
| **B5.7** | Monitoring and improvement ledger | B5.4, B5.14 | node_converge + verifier | Run history, trend and alarm bands with a drift check and trend chart, an experiment ledger (hypothesis, change, before and after with CIs) with a freeze guard, a spend ledger with a cap alarm (D-62, D-65) | US6 |
| **B5.8** | Paraphrase contamination probe | N1.4, A2.1 | node_converge + verifier | The paraphrase set the PRD specified (P4) built by its documented method, deterministic and cached, and a report of original-vs-paraphrase accuracy with a paired CI (D-62) | US6, US10 |
| **B5.9** | Calibration integrity sheet | A2.6, B5.2 | node_converge + verifier | A second labelling sheet with blind repeats and planted items; labeller self-agreement and planted accuracy reported; calibration is not certified below the bar, and the certificate is bound to the run and rubric hashes (D-62) | US3 |
| **B5.10** | v0.4.0 | B5.7, B5.8, B5.9, B5.11, B5.14, B5.15, B5.16 | node_converge + verifier | Version 0.4.0 with the ledger, probe, integrity sheet, security kit and held-out discipline; canary green; owner-tagged release on PyPI (D-62, D-64, D-65) | US6 |
| **B5.11** | Agent security eval kit | C3.4, B5.14 | node_converge + verifier | Attack-case schema with OWASP category, canaries, expected stopping layers and split; k trials and pass^k; stopping-layer attribution; model-free scripted-attacker mode; held-out ids never reported; lifted from assay-pipeline (MIT) and tested on its cases (D-64) | US11 |
| **B5.12** | Per-sentence citation support | A2.4, A2.5 | node_converge + verifier | A citation-judge node over cached claims: per cited sentence, supports or not and its role; citation precision and aim-citation rate with CIs in records, summary and report; benchmark judge cache keys unchanged (D-65) | US2, US6 |
| **B5.13** | Agent and decision scorers | A2.2, S2.4 | node_converge + verifier | Answer-from-rows, clarification, rule-compliance and structured-evidence faithfulness scorers, each tested on its asking project's cases (D-65) | US6, US11 |
| **B5.16** | value_check strict redesign | B5.13 | node_converge + verifier | `value_check` reports `verified` only when a whole field name or alias is bound to its number by a recognised pattern; every other reading is unverifiable; tested against every earlier verifier generator and a fresh one (D-72, D-74) | US6, US11 |
| **B5.14** | Held-out discipline and tamper-evident logs | A2.6, C3.1 | node_converge + verifier | Held-out manifest of hashes and counts, score-once guard, exposure scan, hash-chained append-only log checked in CI, certified calibration tied to a logged line (D-65) | US3, US6 |
| **B5.15** | `sql_match` shape equivalences | C3.4 | node_converge + verifier | Opt-in equivalences (percent_scale, pivot, empty_check, period_number), off by default, recorded beside the strict score; lifted from agentic-analytics' reference with its baseline cases as tests (D-71) | US6, US11 |
