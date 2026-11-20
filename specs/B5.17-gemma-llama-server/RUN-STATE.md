# B5.17 run state

**Now:** BUILDING (2026-11-21), lane `ggh-gemma-example` in worktree `EvalKit-ggh`, from main d49a585. Not pushed; EvalKit's own session reviews the merge.

## History
- 2026-11-21 — node opened for GGH's R4.4 upstream contribution (GGH OWNER D-64). B5.16 was the last node (confirmed by EvalKit's session).
- 2026-11-21 — built (82e2b27, 9ba9825, 14bee2a). Worked example in `docs/plugins.md`; `examples/llama-server/evalkit.yaml` (loaded, routed, dry-run offline in tests; a real `evalkit run --dry-run --n 5` planned $0.00 generate, $0.03 total, no API call); `docs/gemma/` = GGH main_v2 summary/run/charts at GGH 22579b3 (sha256 match) + `source.json` + `index.html` from `render_report` with records=[]; `build_gemma_page.py --check --ggh <GGH checkout>` OK, `--self-test` PASSED (10 cases). Pages: `docs/gemma/**` path, guarded copy to `_site/gemma/`. `ISSUE-aim-rule.md` drafted, not filed. Live-graph counts in `tests/harness/test_eligibility.py` moved to 44 nodes / 213 rows; `tests/test_release.py` skips the repo-relative example command (covered by `tests/test_llama_server_example.py`).
- Gates: ruff check, ruff format --check, pyright 0 errors, pytest 3,267+ passed; `scripts/node_converge.py B5.17` run twice: 12/12 PASS both times. Awaiting the independent verifier and EvalKit's session's merge review.
