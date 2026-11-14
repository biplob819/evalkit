# B2.4 acceptance criteria

**ONB-01** `evalkit init` in an empty folder writes the config, `.env.example`, a task YAML and JSONL template, and `.gitignore` lines; `evalkit run --dry-run` then works on that config; rerunning `init` refuses to overwrite and changes nothing.
**ONB-02** `evalkit doctor` reports Python, SDKs, key names set or missing (never values), network reach (skipped offline), prices age and cache size; exits 2 when a needed key is missing and 0 when ready.
**ONB-03** `evalkit report --example` writes a report from the bundled example with no keys and no network.
**ONB-04** `--dry-run` prints per-node call counts and cost and makes no API calls (B2.2 behaviour re-verified).
**ONB-05** `--max-cost` and `max_cost_usd` stop the run with exit 3 and a resume hint, counting all spend on the run folder (D-45).
**ONB-06** Every command honours exit codes 0/1/2/3/4 and `--json` prints exactly one object, including on errors.
**ONB-07** `.env` in the working folder is loaded; missing-key errors name the exact variable and never print values.
**ONB-08** `run` and `report` print the single-judge, uncalibrated and pending caveats in text and in `--json` (`caveats`).
