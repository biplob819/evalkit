# B5.16 tasks

- [x] 1. Read D-72, D-74, catalog SEV-01, `evidence.py`, the B5.13 tests, docs/plugins.md, the B5.13 spec, the verifier generators and probes
- [x] 2. Spec, criteria, tasks, run state
- [x] 3. Rewrite `value_check` internals (strict binding), keep `render_evidence` and the D-72 shape
- [x] 4. Tests: port the false-claim reproductions, generators, positive bound claims, fuzz, linear time (`tests/scorers/test_value_check.py`); remove the B5.13 value_check tests
- [x] 5. Run gen.py and gen2.py at full size; fuzzers
- [x] 6. Measure fraud-review-bedrock's recorded reasons; hand-check 30 verified
- [x] 7. Docs (plugins.md value_check section)
- [x] 8. Gates: ruff check, ruff format --check, pyright, pytest
- [x] 9. fraud-review-bedrock D-42 mutation test adapted (scratch); stricter rules adopted
- [x] 10. Commit on the lane branch
