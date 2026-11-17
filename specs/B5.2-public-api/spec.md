# B5.2 — Public API 0.3: promote what downstream uses

**Wave 5. Depends on:** B5.1, S2.4. Decisions: D-61 to D-66.

Skeleton written by the lead so the lane can start (D-66); the lane's builder completes Goal, Why, Interface and Out of scope before tests. Inputs collected before the spec are in `inputs.md` when present.

## Capabilities
- **PUB-01**: Internals used by two or more projects get public homes listed in `docs/versioning.md` and `tests/test_public_api.py` [D-61]
- **PUB-02**: Old import paths keep working for one minor release with a `DeprecationWarning` naming the new path; private names used downstream get public replacements [D-61]
- **PUB-03**: The import audit run on every registered project reports no internal names that a public replacement now covers, other than single-project ones listed in the migration guide [D-61]
- **PUB-04**: Public calibration API (ai-visibility, its OWNER D-66): `CalibrationPair(cluster, human, judge)` at any unit (claim, citation, answer) with a cluster key, `cluster_kappa`, `kappa_from`, and a confusion helper (tp/fn/fp/tn, agreement, strictness fn/(tp+fn), leniency fp/(fp+tn), `owner_constant` when human labels are all one value) [D-65]
