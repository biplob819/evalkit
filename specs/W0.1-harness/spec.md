# W0.1 — Repo and harness

**Wave 0. Depends on:** nothing. **Unblocks:** N1.1, and through it every other node.

## Goal
A repo that installs as `evalkit-harness`, runs `evalkit --version`, and enforces the build process: no node starts before its dependencies are green, and no node turns green without proof that passes every gate.

## Inputs
- EvalKit Tech Spec §11 (build harness), §1 (decisions), §10 (gates).
- Kova reference: Kova's harness (private), its graph-eligibility, node-start and node-converge scripts.

## Outputs
- `pyproject.toml` (hatchling, src layout, Python ≥3.11), `src/evalkit/` skeleton with `evalkit --version`, MIT licence, README stub.
- `.github/workflows/ci.yml` (lint, types, tests, build on push and PR, Python × OS matrix) and `release.yml` (build and publish to PyPI on tag by trusted publishing).
- `docs/graph/graph.md` (26 nodes), `docs/graph/catalog.tsv` (144 rows).
- `scripts/verify_graph_eligibility.py`, `scripts/node_start.py`, `scripts/node_converge.py`, `scripts/secret_scan.py`, `scripts/status.py`, all with `--self-test` where they gate.
- `docs/decisions.md` (D-1 to D-14), `docs/lessons.md`, `changelog.md`.

## Out of scope
- Any evaluation code (starts at N1.1).
- The Typer CLI (B2.2) and onboarding commands (B2.4).

## Constraints
- Harness scripts use only the standard library, so they run before any dependency is installed.
- No network calls except `uv build`/`uv pip` in gate 10.
- Reserving the PyPI name moved to B2.5 (OWNER D-20).
