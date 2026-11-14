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
