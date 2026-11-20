# B5.17 acceptance criteria

**GEM-01** (worked example) [GGH OWNER D-64]
1. `docs/plugins.md` has a "Gemma through llama-server" section naming `gemma-e2b-grounded-v2-q4`, the `endpoints` route and the `openai_compatible` adapter.
2. Its server command carries every flag GGH used: `--jinja --reasoning off --reasoning-budget 0 --temp 0 --top-k 1 --seed 7 --parallel 1` (and host 127.0.0.1, port 8083, `--n-gpu-layers 999`, `--threads 8`, `--ctx-size 8192`, `--no-webui`), with llama.cpp release b12421.
3. It states the gotchas: thinking off (llama-server flags; Ollama `--think=false`), one slot queues (`concurrency: {openai_compatible: 1}`), no sampling parameters sent (greedy is the server's job), cache keys name the model and not the URL or GGUF.
4. A test checks each of 1–3 against the doc.

**GEM-02** (example config) [GGH OWNER D-64]
1. `examples/llama-server/evalkit.yaml` loads with `load_run_config` (no network) and its values equal GGH's `configs/evalkit_v2.yaml` for the v2 model (endpoint `http://127.0.0.1:8083/v1`, judge gpt-6.1-sol, extractor gpt-6-luna, structured output, 2048 output tokens, seed 7, `openai_compatible` concurrency 1).
2. The model routes to `openai_compatible`; generation inputs carry no effort for it.
3. The YAML block in `docs/plugins.md` equals the file.

**GEM-03** (/gemma/ page) [GGH OWNER D-64]
1. `docs/gemma/summary.json`, `run.json` and `charts.vl.json` match the sha256 recorded in `source.json` (GGH's `main_v2` files at its commit 22579b3); no records or cases file is in `docs/gemma/`.
2. `docs/gemma/index.html` equals what `scripts/build_gemma_page.py` renders now (byte-identical, deterministic).
3. The page states: gpt-6.1-sol alone as judge with gpt-6-luna extracting; GGH's calibration stamp naming a Claude Opus 5.5 reference labeller and that it is a model's labels; the aim-rule caveat with the GitHub link to `docs/measurements/aim_rule_check.json`; the Hugging Face model and GGUF repo links; run id, n and every model's headline as in `summary.json`.
4. No `pqa-` id, no isolated 7+ digit number, no failure-explorer item; a note says per-case records are not published.
5. `--self-test` shows the check fails on each planted defect (missing statement, case id, failure item, stale page, edited source file, records file present).

**GEM-04** (Pages) [GGH OWNER D-64]
1. `pages.yml` has `docs/gemma/**` in the push paths and copies `docs/gemma/` to `_site/gemma/` inside `if [ -f docs/gemma/index.html ]`.
2. A test checks both, and that the copy happens after `_site` is made.

**Records**
- Spec, criteria, tasks, RUN-STATE; changelog line under Unreleased; `ISSUE-aim-rule.md` drafted, not filed.
- Gates: ruff check, ruff format --check, pyright, pytest; `scripts/node_converge.py B5.17` twice.
