# B5.4 run state

**Now:** GREEN (2026-11-17). v0.3.0 released.

## History
- 2026-11-17 — spec written.
- 2026-11-17 — lane build: version 0.3.0, value_check withheld (D-74), changelog 0.3.0 section, docs/downstream.md, RC canary docs/measurements/downstream-canary-2026-11-17-rc.json, tests/test_v03_release.py.
- 2026-11-17 — built in its lane; the verifier refuted V03-01 on a stale canary record, and its must-fixes (example_gold_row wording, fraud-review's pin) were fixed; the RC canary was re-run on the final candidate. CI's linear-time guards flaked on slow runners (budget raised). The owner approved tag and publish (D-75): v0.3.0 at 4ddfef5, published by trusted publishing, post-release install check green on three OSes; record docs/measurements/release-v0.3.0-2026-11-17.json. The tag's CI canary cloned projects shallow, which failed one course-assistant git-history test; fetch-depth 0 fixed. GREEN.
