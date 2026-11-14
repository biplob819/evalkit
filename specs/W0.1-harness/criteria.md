# W0.1 acceptance criteria

One block per capability. Each criterion is checked by a named test.

**HAR-01 — uv project and package skeleton**
- `uv sync` succeeds; `uv run evalkit --version` prints `evalkit 0.0.1`.
- `pyproject.toml` names the distribution `evalkit-harness`, requires Python ≥3.11, declares the MIT licence and the `evalkit` console script.
- `LICENSE` is MIT. `.pre-commit-config.yaml` runs ruff.

**HAR-02 — CI and release workflows**
- `ci.yml` runs on push and pull request; runs ruff, pyright, pytest and a wheel build; matrix covers Python 3.11–3.13 on Linux, macOS and Windows.
- `release.yml` runs on `v*` tags, builds with uv, and publishes with PyPI trusted publishing (`id-token: write`), no stored token.

**HAR-03 — graph eligibility**
- Fails on a cycle, an unknown dependency, an unparseable `depends_on`, a catalog row naming an unknown node, and a node building or starting ahead of a non-green dependency.
- Passes on the live graph. `--self-test` passes.

**HAR-04 — node start**
- Refuses when the node is unknown, has no catalog rows, a dependency is not green, the spec folder or any of `spec.md`, `criteria.md`, `tasks.md` is missing, or `criteria.md` misses a capability.
- On success writes `specs/<node>/.base` with the git HEAD; `--mark-building` flips the node's rows to building. `--self-test` passes.

**HAR-05 — node converge**
- Runs all 12 gates in order and reports every one even after a failure; exits 1 if any gate fails; writes a log under `.harness/<node>/`.
- `--self-test` shows every gate failing on bad input (and golden SKIP, not PASS, with no tests).

**HAR-06 — records**
- `graph.md` parses to 26 nodes with no cycle; `catalog.tsv` parses to 144 rows, each naming a graph node; every node has rows.
- `decisions.md` holds D-1 to D-14; `lessons.md` and `changelog.md` exist; `scripts/status.py` prints the graph table and a per-node table.

**HAR-07 — secret scan**
- Detects Anthropic, OpenAI, AWS, GitHub and PyPI tokens and private keys; honours the `secret-scan: allow` marker; the live repo is clean.

**Moved:** reserving the PyPI name now belongs to B2.5 (OWNER D-20).
