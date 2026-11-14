# Decision log

Append-only. Each entry: ID, date, decision, alternatives, reason. `OWNER D-n` marks a ruling made by the owner. Grep this file before asking the owner a question.

| ID | Date | Decision | Alternatives considered | Why |
| --- | --- | --- | --- | --- |
| D-1 | 2026-11-14 | A run is a typed DAG of nodes, executed by a small in-house runtime (asyncio + pydantic) | Inspect AI as the engine; LangGraph; Hamilton, Prefect or Dagster | Downstream repos import EvalKit, so it must stay dependency-light and stable. Claim extraction, dual judging and bootstrap aggregation are custom anyway. The cache, resume and replay design stays fully explainable |
| D-2 | 2026-11-14 | Content-addressed node cache keyed on node, code version, inputs, model, params, prompt hash and seed | Plain JSONL checkpoints | One mechanism gives resume, exact rescoring, cheap reruns and the demo replay file |
| D-3 | 2026-11-14 | One fixed, versioned claim extractor feeds both judges | Each judge extracts its own claims | Judge agreement is only meaningful on identical claim sets |
| OWNER D-4 | 2026-11-14 | Opus 5.5 and GPT Sol both judge every claim on the full run | Sol only on the labelled set; Sol on a 25% sample | Self-preference only shows when results are split by generator family |
| D-5 | 2026-11-14 | Calibrate on 100 labels: 50 tuning, 50 held out, plus a 20-answer self-consistency relabel | 50 labels tuned and reported on the same set | A reported kappa must not be training accuracy |
| OWNER D-6 | 2026-11-14 | The live demo takes a question plus a pasted abstract or PMID | Retrieve from PubMed; dataset questions only; ungrounded live mode | Keeps groundedness meaningful with no retrieval step that can fail |
| D-7 | 2026-11-14 | Demo in the shared Streamlit Community Cloud app `portfolio-demos`, spend counter in a hosted KV store | Hugging Face Spaces; in-memory counter | One deployment and secret store; in-memory counters reset when the app sleeps |
| OWNER D-8 | 2026-11-14 | Lean Kova build harness: single builder plus a skeptical verifier | Full Kova; tests only | Dependency-gated, verifiable progress without months of process overhead |
| OWNER D-9 | 2026-11-14 | Publish on PyPI as `evalkit-harness`; CLI `evalkit`; import `evalkit` | GitHub-only install; renaming the project | `evalkit` on PyPI is an unrelated package |
| OWNER D-10 | 2026-11-14 | Dual judging by default; labelled single-judge mode with one provider key | Require both keys; any judge models | One-key users can still evaluate; outputs say self-preference wasn't measured |
| D-11 | 2026-11-14 | No telemetry | Opt-in analytics | Users' data goes only to the providers they configure |
| D-12 | 2026-11-14 | Entry-point plugins for adapters and scorers; OpenAI-compatible adapter with `base_url` | Fork-and-edit | Local models and custom scorers without touching EvalKit |
| D-13 | 2026-11-14 | Build status comes from `scripts/status.py` in this repo, not a `plan-evalkit.tsv` in the statusnow skill | Add a plan file to `~/.claude/skills/statusnow/` | statusnow's `gather.py` is hard-wired to the Kova checkout, so a plan file there would never be read; a repo-local script reads the live catalog and needs no change to the owner's skills |
| D-14 | 2026-11-14 | Build backend hatchling, src layout, Python ≥3.11, dev tools in a uv dependency group | setuptools; flat layout | Hatchling handles PEP 639 licence metadata cleanly; src layout stops tests importing the uninstalled package by accident |
| OWNER D-15 | 2026-11-14 | HAR-08: the owner publishes the 0.0.1 placeholder with `uv publish` from their own terminal; trusted publishing from GitHub is set up for v0.1.0 | Publish through a GitHub release first; defer to B2.5 | Claims the name today without waiting for the repo and PyPI trusted-publisher setup |
| OWNER D-16 | 2026-11-14 | Public GitHub repo `biplob819/evalkit` on the owner's personal account, created now | Private first; work account; later | CI runs from the first push, and trusted publishing needs the repo |
| OWNER D-17 | 2026-11-14 | One local commit per node when it turns green; the changed-tests gate measures from that commit | Commit and push per node; owner commits | Clean per-node history without pushing work the owner hasn't seen |
| D-18 | 2026-11-14 | The sdist ships only `src/evalkit`, README, LICENSE and changelog | Ship the whole repo | The build harness, specs and design docs belong in the repo, not in every download |
| D-19 | 2026-11-14 | Answer labels are task-defined strings (`TaskConfig.labels`), not a hard-coded yes/no/maybe; citations are 1-based | Literal yes/no/maybe in the schema; 0-based citations | User tasks need their own label sets (US10); models are shown "Sentence 4", so 1-based avoids an off-by-one between prompt and scorer |
| OWNER D-20 | 2026-11-14 | HAR-08 (reserve `evalkit-harness` on PyPI) moves from W0.1 to B2.5, the v0.1.0 release node | Block every node until the owner publishes | Keeps the build moving; the owner accepts the small risk of the name being taken first |
