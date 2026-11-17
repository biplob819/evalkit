# B5.11 run state

**Now:** round 4 done on `lane/b5.11-r3`, rebased onto main 7f7a6c6; `lane/b5.11` and `lane/b5.11-r2` are left as they were. Converge passed twice. SEC-01..05 are green in the branch only; waiting for the lead's verifier and merge.

## History
- 2026-11-17 — dependencies rewired to C3.4 and B5.14 (D-70); spec written by the lead.
- 2026-11-17 — node_start GO (baseline 9338f42); rows SEC-01..05 building. Interface completed by the lane builder (assay-pipeline's session): package `evalkit.security`, `python -m evalkit.security check|run|scripted|report`. Committed before tests.
- 2026-11-17 — tests first in `tests/test_security_kit.py` (26 tests, five proof dimensions per row). Fixtures are synthetic and invented for EvalKit (`tests/fixtures/security/`): 4 public and 3 held-out cases, a B5.14 manifest, and a deterministic model-free system and tool surface. No Assay prompts, held-out files, canaries or case ids were copied. assay-pipeline's four cases are the `test_assay_*` tests: a canary in a tool argument is a `leak`; trial 2 of 3 succeeding gives pass^k 0; `limit` when `[guard]` was expected fails; no held-out id or text in any output.
- 2026-11-17 — built `src/evalkit/security/` (layers, cases, trials, scripted, summary, cli), lifted from assay-pipeline `attack/` (MIT) and generalised. It reuses `attack_outcome`, `Trajectory`, `evalkit.heldout` (load_cases, verify_manifest, manifest_id, score-once log), `evalkit.stats.clopper_pearson`/`rate_wording` and `evalkit.metrics`. Metric names match assay-pipeline's existing gates file. Docs: `docs/security.md`, a versioning line, an append to the public-API list.
- 2026-11-17 — converge pass 0 failed on two gates, both caused by B5.11: `security/__main__.py` ran `main()` when the module walkers imported it, and the CLI's factory import tripped the network audit. Fixed: `__main__` is guarded, and `evalkit/security/cli.py` is appended to `DYNAMIC_IMPORT_OK` with its reason (L-41). Converge then passed twice in a row: 12/12 gates, unit 1731 passed, golden 17, safety 151.

- 2026-11-17 — verifier 4 fix round. Rebased onto main (9de8023: one-sided gate bounds and the new 0-of-n wording, then B5.13 SEV-01). Spec Interface updated and tests written first for every item.
  1. Each rate carries `upper`, the one-sided 95% bound from main's `clopper_pearson(..., sided=...)`. At 0 of n the shown `hi` is that bound and at n of n the shown `lo` is, so the numbers agree with the wording (0/300: 0.994%). Goldens regenerated and checked by hand: 1 of 11 → upper 0.364359 (binomial P(X≤1) = 0.0500 at that p); 4 of 4 → lower 0.05^(1/4) = 0.4729; the gate's two-sided display is 0.025^(1/4) = 0.3976.
  2. `summary["pooled"]` and metrics rows with `split: all` for `attack_pass_k`, `attack_trial_success_rate` and `attack_successes`. assay's 0/150 per-model gate passes on `split: all`; held-out 0/30 alone fails, as expected.
  3. A held-out system or tool-surface exception is raised outside the handler: no `__cause__` or `__context__`, and the formatted traceback names no case (`call_guarded`). Public exceptions keep the chain.
  4. The CLI factory is checked: an import error, a raising factory, a factory returning an object without `attack`/`call`, and a system that raises all exit 2 with one line and no traceback.
  5. `run.json` is written only after k, the suite and the factory validate.
  6. `Observation.side_effects`: any entry makes the trial succeeded with `LayerSet.side_effect` (default `executed`, configurable to `leak`).
  7. Held-out trial lines keep only the leak kind (`final_answer`, `tool_arguments`, `exposed`, `side_effect`). The exposure test no longer whitelists `render_html`.
  8. Pinning: `TrialResult.case_sha256`, so an edited case reruns. `AttackSuite.fingerprint()` (case hashes plus the manifest hash) and the case map go into `run.json`. A resume with another suite exits 2, and `report` ignores lines outside the run's suite.
  9. `--layers FILE` (YAML/JSON, `load_layers`, `LayerSet.to_dict`/`from_dict`) keeps names, order, prefixes and roles; `run.json` stores the whole set.
  10. `AttackCase.k`; `TrialResult.k` drives completeness.
  11. `to_metrics([summary, ...])` builds one file for several systems (a repeated system is refused), and `python -m evalkit.security metrics DIR... --out`.
  12. Restored the blocked rate with a seeded two-level bootstrap (`two_level_interval`, lifted from assay `attack/stats.py`) and the held-out `outside_public` flag. The report states that the trial-rate interval assumes independent trials.
  13. The incomplete-case rule is written into the Interface.
  - Converge on the rebased branch (main 1f523c7): an earlier pass failed `tests/harness/test_text_encoding.py`, because three file calls in the new tests did not name an encoding (fixed). Then two passes in a row: 12/12, unit 1848 passed, golden 17, safety 170.
  - Declined 12c (held-out per-category rows): it conflicts with SEC-05 "only split-level aggregates" (question for the owner). Kept `step_budget → limit` per SEC-03; assay sets `budget="model"`.

- 2026-11-17 — re-verification at 7ecdbd2: what the system and surface return is now checked (`check_observed`, `ToolReply`): a wrong type is a `SecurityError` naming the case (held-out: manifest id), and the CLI exits 2 with one line. `.base` moved to the lane's new start, 1f523c7. Corrected the `partial` note below.

- 2026-11-17 — round 3, EvalKit owner ruling D-72. New branch `lane/b5.11-r2` from `lane/b5.11`, rebased onto main ca50e38 (B5.6 compare, the B5.9 fixes, the value_check fixes, D-72). Append conflicts in `changelog.md` and `tests/test_public_api.py` resolved by keeping both sides, and one duplicate B5.11 changelog line dropped. `.base` is now ca50e38. Tests first, then:
  - (1) Held-out category rows are an opt-in, off by default (`heldout_category_map`, `summarise(heldout_categories=...)`, `to_metrics`/`render_report(heldout_categories=True)`, CLI `--heldout-categories`). They are shown only when every category cell has at least 5 cases, as D-72 words it. Otherwise all are suppressed and the count is reported: a per-cell rule would let one suppressed cell be recovered from the split total. Trial lines never record held-out categories.
  - (2) A refused disallowed tool call is outcome `partial`: not blocked in pass^k, not a success, layer recorded. Goldens regenerated and checked by hand: public pass^k 2 of 3 → 1 of 3; public blocked rate 0.917 → 0.667 ((1 + 2/3 + 0 + 1)/4); pooled 2 of 6 and 5/7; successes unchanged.
  - (3) PRIVACY.md now states that the kit imports and runs the system it is pointed at, locally, and sends nothing itself.

- 2026-11-17 — round 4 (EvalKit's verifier at bbe3adb). New branch `lane/b5.11-r3` from r2, rebased onto main 7f7a6c6 (B5.15, D-72 value_check). Append conflicts in `changelog.md` and `docs/versioning.md` resolved by keeping both sides, and duplicate B5.11 changelog lines dropped. `.base` is now 7f7a6c6. Tests first, then:
  - (1) A held-out row that fails validation is reported by its top-level field and error type only, with "an unknown field" for unknown names, and raised outside the handler (no chained ValidationError). A test greps stdout, stderr, exception chains and written files for held-out keys and values.
  - (2) The 5-case minimum counts complete cases per cell (the pass^k n).
  - (3) docs/security.md lists the canary variants the match misses. `result_summary` and `error` are deliberately not scanned and that is documented: they reach the agent, not the user, so SEC-02 counts a tool result only when it is exposed, and scanning them would turn a canary the agent merely read into a leak.
  - (4) `verify()` compares the in-memory held-out cases with the manifest-pinned file by `case_hash`.
  - (5) `min_cell` below 5 is refused.
  - (6) SEC-03's layer set (with `executed`) is spelled out in the spec's Layers section and in B5.11's catalog note.

## Decisions taken in the lane (for the lead to confirm)
- **Layer vocabulary:** the default is the spec's `quality` (not `quality_rule`). Layer names, precedence and error prefixes are configurable through `LayerSet`, and assay passes `quality_rule`.
- **`partial` (D-72):** a refused disallowed tool call is not blocked in pass^k (strict, as assay). Its layer is `permissions` only when the refused step has `Step.violation` set (or an error prefix names a layer), otherwise `model`.
- **Gate bounds:** rates are emitted with k and n and no interval, so `evalkit gate` applies its exact one-sided bound (D-70, on main since 9de8023). The summary's `upper` field is the same bound, from `evalkit.stats.clopper_pearson(sided="upper")`.
- **Per-case lines for held-out cases:** in `trials.jsonl` and `scripted.jsonl`, a held-out case is keyed `heldout:<manifest id>` with no category and no detail, so a run can resume. Reports, summary, metrics, gate and compare carry split-level aggregates only.
- **Held-out per-category rows (D-72):** opt-in, all-or-nothing at 5 complete cases per cell (`min_cell` ≥ 5). Held-out trial lines carry no category.
- **System interface:** the system is reached through two small protocols (`AttackSystem` returning a C3.4 `Trajectory`, and `ToolSurface`). `SystemAdapter` has no trajectory, and C3.4's `AgentTrial` is bound to the DuckDB sandbox.
