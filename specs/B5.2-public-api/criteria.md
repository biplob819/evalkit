# B5.2 acceptance criteria

**PUB-01** Internals used by two or more projects get public homes listed in `docs/versioning.md` and `tests/test_public_api.py` [D-61]
**PUB-02** Old import paths keep working for one minor release with a `DeprecationWarning` naming the new path; private names used downstream get a public replacement or a stated reason (in the spec) [D-61]
**PUB-03** The import audit run on every registered project reports no internal names that a public replacement now covers, other than single-project ones listed in the migration guide [D-61]
**PUB-04** Public calibration API (ai-visibility, its OWNER D-66): `CalibrationPair(cluster, human, judge)` at any unit (claim, citation, answer) with a cluster key, `cluster_kappa`, `kappa_from`, and a confusion helper (tp/fn/fp/tn, agreement, strictness fn/(tp+fn), leniency fp/(fp+tn), `owner_constant` when human labels are all one value) [D-65]
