# B2.2 — CLI

**Wave 3. Depends on:** A2.6, B2.1. **Unblocks:** B2.4, D3.2.

## Goal
One command runs the whole benchmark graph and leaves a run folder anyone can audit; the same command resumes after a crash, a cost stop or a pending judge without paying twice. Calibration and reporting are commands too.

## Commands (`evalkit/cli.py`, `evalkit/run.py`)
- `evalkit run CONFIG [--task NAME|PATH] [--n N] [--out DIR] [--max-cost USD] [--dry-run] [--wait MIN] [--live-judges] [--json]` (CLI-01):
  - Loads config and task, samples cases (seeded, stratified), then generate → validate → extract → judge (both judges, batch by default, OWNER D-44) → score → aggregate → `records.jsonl`, `summary.json`, `report.html`, `charts.vl.json` in the run folder.
  - The run folder name is derived from the config hash, task, seed and n, so rerunning the same command resumes from the content-addressed cache.
  - A progress line per stage shows done/total and running cost; `--json` prints one final JSON object instead.
  - `--dry-run` prints call counts and the cost estimate per stage and makes no API calls.
  - Exit codes: 0 pass, 1 threshold failed, 2 config/runtime error, 3 cost cap (resume hint), 4 judging pending (resume hint).
  - `--live-judges` judges through the normal API with the same cache keys as batch judging.
- `evalkit calibrate export|label|import|score RUN_DIR` (CLI-02): wraps A2.6 (`export` writes the sheet and CSV; `label [--relabel]` serves the D-40 page on 127.0.0.1 and prints the URL; `import FILE.csv` reads a filled CSV; `score` writes `calibration.json`, refusing a held-out set reused under a new rubric).
- `evalkit report RUN_DIR [--publish]` (CLI-03): re-renders the report from the run folder (and `calibration.json` when present); `--publish` applies the B8 guard.
- Validation (CLI-04): unknown flags, missing files, bad YAML, unknown models (no price, no endpoint), unknown threshold metrics, missing provider keys for the selected models and judges, and `--n` larger than the dataset all fail before any API call with one clear message and exit 2; keys are never printed.
- User tasks (CLI-05): `--task path/to/task.yaml` with a local JSONL dataset (`{id, question, context: [..], label}` per line) runs end to end with the same graph.
