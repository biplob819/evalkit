# Lessons

Numbered L-n. Each lesson names the failure, the rule it produced, and where that rule is enforced.

| ID | Lesson | Rule | Enforced by |
| --- | --- | --- | --- |
| L-1 | A gate that has never failed is a gate nobody has checked (carried over from Kova) | Every gate ships a `--self-test` that feeds it bad input and expects FAIL | `node_converge.py --self-test`, `tests/harness/` |
| L-2 | W0.1 verifier: the proofs gate accepted a test name found only in a comment, a string, a prefix or a skipped test | Proofs resolve through `ast` to a real, unskipped `def test_*` under `tests/`; `logical` can never be n/a | `node_converge.proof_problems` |
| L-3 | W0.1 verifier: a failed `git diff` against a bad `.base` was ignored, so gate 11 passed | A tool error inside a gate is a FAIL, never "no changes" | `node_converge.changed_files` |
| L-4 | W0.1 verifier: gate self-tests passed even when ruff, pyright or pytest were not installed | A self-test must require the planted defect's identity (rule code, file, test name) in the output, not just a non-zero exit | `node_converge.self_test` |
| L-5 | W0.1 verifier: running node-start in check-only mode silently moved the changed-tests baseline | Check-only commands never write; baselines are written once, by `--mark-building`, and only `--reset-base` overwrites them | `node_start.start` |
| L-6 | Local Python is 3.12 but the floor is 3.11; a backslash in an f-string expression slipped in | pyright runs with `pythonVersion = "3.11"`, and CI tests 3.11 | `pyproject.toml`, `ci.yml` |
| L-7 | First CI run: Windows checkout turned LF into CRLF, so the byte-identical catalog round-trip failed on all three Windows jobs | `.gitattributes` forces LF for every text file | `.gitattributes`, `test_catalog_round_trip_is_byte_identical` on windows-latest |
| L-8 | N1.1 verifier: pydantic `frozen=True` is shallow; `record.claims.append(...)` changed the object's JSON | Sequence fields are tuples, mappings are `FrozenDict`, free-form JSON goes through `deep_freeze` | `evalkit/schemas/_base.py`, `test_records_are_deeply_immutable_and_hashable` |
| L-9 | N1.1 verifier: pydantic floats accept infinity, which dumps as `null` and fails to load back | `allow_inf_nan=False` on the base model and every float type | `test_non_finite_floats_are_rejected` |
| L-10 | N1.1 verifier: a test that loops over `__all__` can never find a missing export | Contract tests compare against an independent list taken from the spec | `test_every_tech_spec_schema_is_exported` |
