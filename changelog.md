# Changelog

Format: `[TYPE] [NODE] — summary`. Types: FEAT, FIX, DOCS, HARNESS, RELEASE.

## 2026-11-14

- [HARNESS] [W0.1] — Repo skeleton (`evalkit-harness`, src layout, Python ≥3.11), CI and release workflows, build graph (26 nodes) and capability catalog (144 rows), `verify_graph_eligibility.py`, `node_start.py`, `node_converge.py` (12 gates), `secret_scan.py`, `status.py`, every gate with a self-test, decision log seeded D-1 to D-14.
- [FIX] [W0.1] — Verifier findings: AST-checked proofs under tests/, git-diff errors fail gate 11, check-only node-start writes nothing, self-tests require the defect's identity, wider secret patterns, strict graph and catalog parsers.
- [HARNESS] [W0.1] — Green. HAR-08 (PyPI name) moved to B2.5 (OWNER D-20).
- [FEAT] [N1.1] — `evalkit.schemas` (results format v1: benchmark, summary, calibration, config, agent), `evalkit.config` YAML loaders with file-and-field errors, v1 fixtures and round-trip contract tests.
- [FIX] [N1.1] — Verifier findings: finite floats, required well-formed schema_version, honest RunSummary stamps, deep immutability, strict ints, linked claims and verdicts, loader errors that name the file, duplicate-key YAML rejected.
- [FEAT] [N1.1] — Green.
