# B5.11 run state

**Now:** built on `lane/b5.11`; converge passed twice; SEC-01..05 marked green in the branch only. Waiting for the lead's independent verifier and merge (the owner sets green at merge).

## History
- 2026-11-17 — dependencies rewired to C3.4 and B5.14 (D-70); spec written by the lead.
- 2026-11-17 — node_start GO (baseline 9338f42); rows SEC-01..05 building. Interface completed by the lane builder (assay-pipeline's session): package `evalkit.security`, `python -m evalkit.security check|run|scripted|report`. Committed before tests.
- 2026-11-17 — tests first in `tests/test_security_kit.py` (26 tests, five proof dimensions per row). Fixtures are synthetic and invented for EvalKit (`tests/fixtures/security/`): 4 public and 3 held-out cases, a B5.14 manifest, and a deterministic model-free system and tool surface. No Assay prompts, held-out files, canaries or case ids were copied. assay-pipeline's four cases are the `test_assay_*` tests: a canary in a tool argument is a `leak`; trial 2 of 3 succeeding gives pass^k 0; `limit` when `[guard]` was expected fails; no held-out id or text in any output.
- 2026-11-17 — built `src/evalkit/security/` (layers, cases, trials, scripted, summary, cli), lifted from assay-pipeline `attack/` (MIT) and generalised. It reuses `attack_outcome`, `Trajectory`, `evalkit.heldout` (load_cases, verify_manifest, manifest_id, score-once log), `evalkit.stats.clopper_pearson`/`rate_wording` and `evalkit.metrics`. Metric names match assay-pipeline's existing gates file. Docs: `docs/security.md`, a versioning line, an append to the public-API list.
- 2026-11-17 — converge pass 0 failed on two gates, both caused by B5.11: `security/__main__.py` ran `main()` when the module walkers imported it, and the CLI's factory import tripped the network audit. Fixed: `__main__` is guarded, and `evalkit/security/cli.py` is appended to `DYNAMIC_IMPORT_OK` with its reason (L-41). Converge then passed twice in a row: 12/12 gates, unit 1731 passed, golden 17, safety 151.

## Decisions taken in the lane (for the lead to confirm)
- **Layer vocabulary:** the default is the spec's `quality` (not `quality_rule`). Layer names, precedence and error prefixes are configurable through `LayerSet`, and assay passes `quality_rule`.
- **`partial` counts as blocked:** `attack_outcome`'s `partial` (a disallowed tool refused) is a blocked trial, attributed to `permissions`. assay counted it as not blocked in pass^k; the spec defines success as "the attack got through".
- **Gate bounds:** rates are emitted with k and n and no interval, so `evalkit gate` computes the bound itself. On main, `gates._interval` still uses two-sided `clopper_pearson`. D-70's one-sided bound is not implemented there yet, and `evalkit.stats` has no one-sided function. Once the gate applies D-70, the kit's metrics get it unchanged.
- **Per-case lines for held-out cases:** in `trials.jsonl` and `scripted.jsonl`, a held-out case is keyed `heldout:<manifest id>` with no category and no detail, so a run can resume. Reports, summary, metrics, gate and compare carry split-level aggregates only.
- **Not lifted:** the two-level bootstrap. pass^k uses an exact interval over cases, and the trial rate is labelled as treating trials as independent.
- **System interface:** the system is reached through two small protocols (`AttackSystem` returning a C3.4 `Trajectory`, and `ToolSurface`). `SystemAdapter` has no trajectory, and C3.4's `AgentTrial` is bound to the DuckDB sandbox.
