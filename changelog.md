# Changelog

Format: `[TYPE] [NODE] — summary`. Types: FEAT, FIX, DOCS, HARNESS, RELEASE.

## 2026-11-14

- [HARNESS] [W0.1] — Repo skeleton (`evalkit-harness`, src layout, Python ≥3.11), CI and release workflows, build graph (26 nodes) and capability catalog (144 rows), `verify_graph_eligibility.py`, `node_start.py`, `node_converge.py` (12 gates), `secret_scan.py`, `status.py`, every gate with a self-test, decision log seeded D-1 to D-14.
- [FIX] [W0.1] — Verifier findings: AST-checked proofs under tests/, git-diff errors fail gate 11, check-only node-start writes nothing, self-tests require the defect's identity, wider secret patterns, strict graph and catalog parsers.
- [HARNESS] [W0.1] — Green. HAR-08 (PyPI name) moved to B2.5 (OWNER D-20).
