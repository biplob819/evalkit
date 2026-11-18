# B5.5 — Cross-project eval index

**Wave 5. Depends on:** B5.1 (registry `docs/downstream.tsv`), B5.3 (metrics-1, gates, efficiency names), B5.6/B5.7 (compare-1 rows in the experiment ledger), 0.4.0 strict run-folder detection. **Asked by:** OWNER D-61/D-62 (one view of every project), D-67 (EFF-04), the portfolio site (inputs.md). Decisions: D-61, D-62, D-63, D-67, D-70, D-73..D-79.

## Goal
One page on the EvalKit site that shows, for EvalKit and every registered project, the number each project stands behind (with its interval), whether its judge is calibrated, whether its gates hold, which EvalKit made it and when, and per model what quality costs — built only from local files, so it never calls a model or the network, and it never drops, guesses or leaks.

## Two steps, kept apart

1. **Snapshot (explicit, documented, may use the network).** `evalkit index snapshot` fetches, per registered project, the registered summary file (and the few named companions below) at one commit with `git fetch --depth 1` and writes a local snapshot folder. It is the only step that touches a remote and it never runs in tests over the network (tests fetch from local git repositories made in `tmp_path`).
2. **Build (offline).** `evalkit index build` reads the registry, the index config and the snapshot folder — local files only — and writes `index.html` and `index.json`. No model call, no network, no clock (the output carries no build time, so the same inputs give the same bytes).

## Exact interface

Module `evalkit.index` (public; its `__all__` is the contract):

- `INDEX_SCHEMA = "index-1"`, `SNAPSHOT_SCHEMA = "index-snapshot-1"`, `CONFIG_SCHEMA = "index-config-1"`, `EXIT_OK = 0`, `EXIT_MISSING = 1`, `EXIT_INPUT = 2`, `SELF = "evalkit"`.
- `RegistryRow(project, repo, branch, pin, summary)`; `read_registry(path) -> list[RegistryRow]` (tab-separated, header must hold `project`, `repo`, `branch`, `pin`, `summary`; `-` means none; a duplicate or blank project, or a project name outside `[a-z0-9][a-z0-9._-]*`, is refused).
- `Selector(metric, labels={}, label=None)`, `ProjectConfig`, `IndexConfig`; `load_config(path) -> IndexConfig`, `parse_config(text) -> IndexConfig` (strict YAML, unknown keys refused).
- `snapshot(registry, out, *, config=None, sources=None, commits=None, root=None, now=None) -> list[SnapshotResult]`; `SnapshotResult(project, status, commit, reason, files)`.
- `read_project(folder, row, config) -> ProjectEntry` (never raises); `build_index(registry, snapshots, config=None) -> Index` (never raises for a project's files).
- `render_html(index) -> str`, `index_json(index) -> str` (sorted keys, indent 2, LF), `write_index(index, out) -> list[Path]` (atomic).
- Data: `Figure` (one metric row as shown), `ModelRow`, `GateStatus`, `ProjectEntry`, `Index` (each with `as_dict()`), `IndexInputError` (bad registry, config or output folder).
- CLI: `evalkit index snapshot [--registry docs/downstream.tsv] [--config configs/index.yaml] [--out docs/index/snapshots] [--source PROJECT=REPO]... [--commit PROJECT=SHA]... [--json]` and `evalkit index build [--registry docs/downstream.tsv] [--config configs/index.yaml] [--snapshots docs/index/snapshots] [--out docs/index] [--require-all] [--json]`. Exit codes: 0 built (missing projects shown); 1 with `--require-all` when any project is missing; 2 bad registry, config or unwritable output.

### Snapshot folder
`<out>/<project>/snapshot.json` (`schema_version: index-snapshot-1`, `project`, `repo`, `source` (the URL, or `local` for a local path), `ref`, `commit` (40-hex, the commit the files were read at), `fetched_at` (UTC, from `now`), `status: ok|missing`, `reason`, `evalkit_pin` (the registry's pin cell), `evalkit_version` (EvalKit's own row: the `version` in `pyproject.toml` at that commit), `files: {role: {path, name, sha256}}`) plus each fetched file under its role's name. Roles: `summary` (registry `summary` cell, or the config's override), `run` (`run.json` beside an EvalKit `summary.json`, when present), `gates`, `experiments` (config paths; optional), and `lessons` (existence only: never copied). Paths must be relative, inside the repo, without `..`, and never a `.env` file (refused before any git call). EvalKit itself is the eighth project (`SELF`): read from the local repository (`root`, default: the current one) at `HEAD` unless `--commit evalkit=SHA`, path `docs/results/summary.json`. A project whose fetch fails gets `status: missing` and a fixed reason (`git fetch failed (exit N)`, never git's stderr).

### Index config (`configs/index.yaml`)
```yaml
schema_version: index-config-1
heldout_splits: [heldout]          # always includes "heldout"
projects:
  assay-pipeline:
    summary: docs/results/main-2026-11-18/metrics.json   # overrides the registry cell
    headline: {metric: attack_pass_k, labels: {model: pipeline, split: public}, label: "Attacks stopped"}
    quality: {metric: normal_answer_correct_rate, labels: {split: public}}   # per model
    calibration: {metric: judge_hand_label_agreement, labels: {judge: claude-opus-5-5}}
    efficiency_labels: {mode: live}  # narrows the efficiency rows of each model
    gates: gates.yaml                # path in the project's repo
    experiments: ledger/experiments.jsonl
    lessons: docs/lessons.md
    kinds: {attack_successes: guardrail}   # monitoring kind per metric
```

### What each project shows (IDX-01)
Read in this order, each a reason on failure (IDX-02): snapshot.json present and valid → status ok → summary file present, sha256 equal to the snapshot's → UTF-8 strict JSON → `metrics-1` (validated in full) or an EvalKit `summary.json` (strict marker, `evalkit.runfolder`; then `RunSummary`, converted with `evalkit.metrics.from_run_summary`). Then:
- **Headline:** the config's `headline` selector must match exactly one metric row; shown with its value, 95% interval and method (or `n`/`k` with `rate_wording` for an exact 0 or 1), the unit, the label. No selector: `not designated` (never picked by the index). Zero or several matches: the problem is shown (`matches no metric`, `matches N rows`).
- **Calibration stamp:** an EvalKit summary: `calibrated` with each judge's held-out kappa and interval and claim count, or `uncalibrated`; a metrics-1 file: the config's `calibration` selector shown as `stated: <metric>`, else `not stated`.
- **Gate status:** the snapshot's gates file through `evalkit.gates.evaluate` (with `RunStatus` for an EvalKit summary: pending → 4, cost cap → 3, read from `run.json` when fetched); an EvalKit summary without one uses `evalkit gate`'s default (every model passes its thresholds); a metrics-1 file without one is `not gated`. Status `pass|fail|error|judging pending|stopped at the cost cap|not gated`, exit code, failed and warned counts, gate rows (already folded to split level for held-out splits).
- **EvalKit version:** metrics-1 `provenance.evalkit`, or EvalKit's own `pyproject.toml` version at the commit; otherwise `not recorded in the run` with the registry pin beside it.
- **Run date:** metrics-1 `created_at` / summary `finished_at` (else `started_at`), shown as the date and the full timestamp; also `run_id`, commit (short, linked), source path, `simulated` (a `SIMULATED` badge, D-63), and EvalKit's stamp lines (single judge, uncalibrated, judging pending, missing verdicts).

### Missing (IDX-02)
A project is always listed: in registry order, then EvalKit last. Missing means one of: no summary registered (`-`), no snapshot, unreadable snapshot.json, snapshot `status: missing`, file absent, file changed since the snapshot (sha256), not UTF-8, not JSON, another project's own format (the run-folder marker reason), metrics-1 or `RunSummary` invalid (first 3 problems, never values), or an unexpected error (`unexpected <Type>`). A missing project's overview row spans its columns with `missing` and the reason, and its section repeats them; nothing is carried over from another project or a default. A broken gates, run.json or experiments file does not hide the summary: its own part shows the reason. `--require-all` exits 1 when any project is missing.

### Efficiency per model (EFF-04)
For every value of the `model` label (in first-seen order) among the efficiency rows (EFFICIENCY_NAMES, narrowed by `efficiency_labels`) and the quality selector's rows: quality (the `quality` selector with `model` added), and cost per correct, cost per case, tokens in/out/reasoning/cached per case, cache-hit share, latency p50/p95/p99, error rate (k of n), retry rate, system cost, eval overhead, mode and price date, each with its interval where the file has one. A pre-0.3 EvalKit summary (no `efficiency`) falls back to its `cost_per_correct_usd`, `cost_per_100_usd` ÷ 100 and `latency_p50_ms`/`latency_p95_ms`, marked `from summary (no interval)`. A name matching several rows for one model shows `ambiguous (N rows)`, never one of them. **Cost per correct is the comparison metric:** rows are ordered by its point value (unknown last, then first-seen order); the lowest is marked `lowest cost per correct`, and `clear` only when its interval's upper bound is below every other model's lower bound (else `intervals overlap`). A project with no `model` label shows `no per-model efficiency in this summary`.

### index.json for the portfolio (inputs.md)
`{schema_version: index-1, projects: [ProjectEntry...]}`; each entry has the fields above plus `story` in the portfolio's shapes:
- `headline: {value, label, note}` — value formatted with its unit; note `95% CI lo–hi (method); run <run_id>; commit <short>; EvalKit <v>`.
- `evals: [{name, data, method, result, status, url}]` — the headline first, then each gate row. Status: a gate row `pass`/`fail`; a warn row `provisional`; an error row `pending`; pending judging or cost cap `pending`; the headline `final` when calibrated (EvalKit) or not simulated (metrics-1), else `provisional`; a simulated project's `pass` becomes `provisional`. `url` links the summary file at the commit.
- `improvements: [{metric, before, after, change, url}]` — each `experiment_closed` entry of the snapshot's experiments ledger: its primary row's old, new and change with the interval (compare-1).
- `learnings: [{lesson, now, url}]` — one entry linking the lessons file at the commit when the snapshot found it. Lesson text is not copied (free text from another repository is never echoed).
- `monitoring: [{metric, kind, target, current, how}]` — each gate row: `kind` from the config's `kinds`, else `north-star` for the headline metric, `cost` for a currency unit or an efficiency cost name, `guardrail` for a count gated `<=`/`==`, `quality` otherwise; `target` `op value (bound)`; `current` the observed value; `how` `evalkit gate (gates file at <short>)`.

### Safety
- Built from local files only; `evalkit.index` imports no network or provider module except `subprocess` for git in `snapshot`.
- Held-out: a selected row whose `split` label is held-out (config `heldout_splits` ∪ `heldout` ∪ the gates file's) shows only `split`, `model`, `judge`; a selector matching held-out rows below split level is refused. Gate rows are folded by `evalkit.gates`.
- No ids: metrics-1 refuses id-named labels and id-shaped values; every shown string passes `safe_text` (64 characters) and a secret filter (Anthropic, OpenAI, AWS, GitHub, PyPI key shapes → `<hidden>`); every HTML value is escaped. Case ids, answers and claim text are never read from the summary (RunSummary holds none; metrics-1 holds none).
- An unreadable file never crashes the page: each project is read inside a catch-all; its failure is its own `missing` row.

## Tests from the asking projects
Committed fixtures in `tests/fixtures/index/`, synthetic, shaped like each project's real output (shapes read from their files read-only; no values copied): course-assistant (EvalKit summary, single model, slices, uncalibrated), gemma-grounded-health (EvalKit summary before 0.3, four models, no efficiency), respectful-offers (metrics-1, simulated, NTD, policy/variant labels, no model label), assay-pipeline (metrics-1, models, public and held-out splits, efficiency block, gates file, experiments ledger), agentic-analytics (its own `summary.json` format: missing), ai-visibility (no summary registered: missing), fraud-review-bedrock (fetch failed: missing), EvalKit (summary with efficiency and calibration, run.json). Goldens `expected/index.html` and `expected/index.json` byte for byte.

## Out of scope
Fetching anything during `build`; computing metrics from raw outputs; trends across runs (B5.7 pages); copying lesson text; writing into downstream repos.
