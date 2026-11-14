# B2.2 acceptance criteria

**CLI-01** `evalkit run` on the fixture task with scripted adapters writes records, summary, report and charts; a second identical invocation makes no model calls and produces identical files; `--dry-run` makes no calls and prints per-stage counts and cost; `--json` prints one parseable object; exit codes 0/1/3/4 are produced by passing, failing-threshold, cost-capped and judging-pending runs; `--live-judges` produces the same verdict cache as batch judging.
**CLI-02** `calibrate export` then `label` (driven through HTTP) or `import`, then `score`, produces `calibration.json`, and the next `report` shows the run as calibrated; `score` refuses a reused held-out set after a rubric change.
**CLI-03** `evalkit report RUN_DIR` re-renders byte-identical HTML from the folder; `--publish` exits 2 with the guard's message for single-judge or uncalibrated runs.
**CLI-04** Each invalid input (unknown flag, missing config, bad YAML, unknown model, unknown threshold metric, missing key, n too large) exits 2 before any model call with a one-line message naming the problem; no key value ever appears in output.
**CLI-05** A user YAML task with a local JSONL dataset runs end to end and its summary names the task.
