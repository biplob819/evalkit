# B5.8 — Paraphrase contamination probe

**Wave 5. Depends on:** N1.4, A2.1. Decisions: D-62, D-66, D-68. Method: DATA_CARD.md (DAT-06).

Skeleton written by the lead (D-66); Goal, Why, Interface and Out of scope completed by the lane builder.

## Capabilities
- **PAR-01**: Paraphrase set built by the PRD's documented method (P4): deterministic, cached, with its prompt and model recorded and cost estimated before any call [D-62]
- **PAR-02**: Contamination report: original-vs-paraphrase accuracy per model with a paired CI [D-62]

## Set (D-68)
Adopt gemma-grounded-health's set (commit c7d70d2) with provenance; owner spot-check of 20 kept and all rejected before publishing results.

## Goal
Measure how much a model's PubMedQA accuracy depends on the exact wording it may have memorised: the same 100 PQA-L questions, contexts and labels, asked once in the original wording and once paraphrased, with a paired interval on the accuracy drop per model.

## Why
PubMedQA has been public since 2019 and is probably in every compared model's training data (PRD contamination risk, DATA_CARD caveat). The PRD (P4) promises a paraphrase probe; gemma-grounded-health built the set by hand with EvalKit's documented method (its D-42) and D-68 adopts it, so every project can run the probe as two ordinary runs and read one report. The builder ships too, so the set can be rebuilt or extended by the same method without hand work.

## Interface
Module `evalkit.paraphrase` (public, semver; in `scripts/check_public_api.py`).

### The vendored set (PAR-01)
- `src/evalkit/tasks/pubmedqa_paraphrase.jsonl` (100 kept) and `pubmedqa_paraphrase_rejects.jsonl` (1 rejected): byte-for-byte gemma-grounded-health c7d70d2, every field kept (`pubid`, `reference_label`, `original`, `paraphrase`, `paraphrase_model`, `paraphrase_prompt_hash`, `check_verdict`, `check_model`, `check_prompt_hash`, `check_reason`).
- `src/evalkit/tasks/pubmedqa_paraphrase.provenance.json`: source repo and commit, sha256 and row count of each file, method (DATA_CARD DAT-06), sample (PQA-L, stratified, seed 7, n 100, 55/34/11), paraphrase and check models with prompt hashes, build spend, licence credit, and `spot_check.status` (`pending` | `passed` | `failed`).
- `ProbeRecord`, `Provenance`, `ProbeSet(kept, rejected, provenance, folder)` (frozen models); `load_set(folder=None)` (the vendored set by default; refuses a file whose sha256 or row count differs from the provenance), `load_provenance(folder=None)`, `jsonl(records)` (the vendored serialisation), `SET_FILES`; `ProbeError` (a `ValueError`).
- `probe_problems(cases, probe, *, n=None, seed=None) -> list[str]` (n and seed default to the provenance sample): every pubid exists in PQA-L with the same original question and label; recorded prompt hashes match the prompts and models match the provenance; the kept label mix equals the sampler's allocation; replaying the method (EvalKit's `stratified_sample` at the seed, rejected questions replaced from the same label's seeded order) with the recorded verdicts reproduces exactly the kept and rejected lists, in order.
- `probe_cases(cases, wording, probe=None)` with `wording` `"original"` or `"paraphrase"`: the 100 cases with PQA-L's ids (`pqa-<pubid>`), contexts and labels, differing only in the question. Raises `ProbeError` when `probe_problems` finds anything.
- Built-in tasks `pubmedqa-paraphrase` and `pubmedqa-paraphrase-original` (datasets `evalkit/pubmedqa-paraphrase[-original]`, same prompt template, labels and scorers as `pubmedqa`). Each load re-verifies the set against PQA-L. Run both with `--n 100`.

### The builder (PAR-01)
- `PARAPHRASE_PROMPT`, `CHECK_PROMPT`, `PARAPHRASE_MODEL = "gpt-6.1-terra"`, `CHECK_MODEL = "claude-opus-5-5"`, `prompt_hash(text)` (first 16 hex of sha256; the recorded hashes are `1bb383c050490b51` and `2331517ce31052a8`).
- Node `ProbeCall` (`paraphrase_probe_call`, input `ProbeCallInput`): one paraphrase or check call through a `ModelAdapter` (effort `low`), cached in the content-addressed store by prompt, model and effort, with an upper-bound `estimate_cost` for the spend cap.
- `estimate_build(cases, prices, *, n, seed, ...) -> float`: the upper-bound USD for the first `n` questions (one paraphrase and one check each).
- `build_probe(cases, adapters, prices, store, *, n=100, seed=7, max_cost_usd=..., echo=print) -> BuildResult`: prints the estimate and cap before any call, then runs rounds (paraphrase all, check all, replace rejects from the label's seeded order) until `n` are kept. Only a `SAME` reply whose paraphrase differs from the original is kept; anything else is rejected with its reason. Deterministic for the same replies; a rerun replays from the store at no cost; stops with `BudgetExceeded` at the cap with finished calls cached.
- `write_probe(out_dir, result) -> Provenance`: writes the two JSONL files (same serialisation as the vendored ones) and a provenance record with `spot_check.status = "pending"`.
- `python -m evalkit.paraphrase verify` checks the vendored set against PQA-L; `python -m evalkit.paraphrase build --out DIR [--n 100] [--seed 7] [--max-cost 2] [--store .evalkit/store] [--dry-run] [--json]` rebuilds or extends (larger `--n`; cached questions are free).

### The report (PAR-02)
- `contamination_report(original_run, paraphrase_run, *, probe=None, n_resamples=10_000, seed=7) -> ContaminationReport` (one `ModelContamination` per model): pairs the two run folders on probe pubids (the original run may be any PubMedQA run containing them; the paraphrase run must hold only probe cases), checks each run used the wording it claims and identical contexts and labels, and per model in both runs gives accuracy (`label_correct`, so pending judging does not matter) on each wording with a 95% bootstrap interval, the paired drop `original - paraphrase` with a 95% paired bootstrap interval and p-value (EvalKit's `Resamples` and `paired`, 10,000 resamples, seed 7), and `flagged` when the interval excludes zero (`direction` `drop`, a contamination signal, or `rise`).
- `ContaminationReport.status` is `provisional` unless the provenance spot-check is `passed`; a `failed` spot-check refuses the report. `report_text(report)` leads with the status. `require_final(report)` raises `ProbeError` unless final.
- CLI: `evalkit report --paraphrase ORIGINAL_RUN PARAPHRASE_RUN [--json] [--publish]` prints the report and writes `contamination.json` into the paraphrase run folder; `--publish` refuses (exit 2) while the spot-check is not `passed`.

## Rules
- No API call in tests; the builder is tested only with scripted adapters. The estimate is printed before the first call.
- The vendored files are never rewritten by the code; `write_probe` refuses to write into the package's tasks folder.

## Out of scope
- Doing the owner's spot-check (task 9; it flips `spot_check.status` in the provenance by hand).
- Probe sets for other datasets (SciFact, user tasks), and dashboard charts of the report (the JSON is the interface).
- Running the probe on real models (a later paid run by the lead).
