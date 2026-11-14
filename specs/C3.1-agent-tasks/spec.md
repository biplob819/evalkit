# C3.1 — Agent tasks

**Wave 3. Depends on:** N1.1, N1.3. **Unblocks:** C3.2.

## Goal
EvalKit ships its own agent suite (OWNER D-33): a small synthetic analytics database and 40 tasks in the shared YAML format — 20 analytics (10 ambiguous, with a hidden intent only the simulated user knows) and 20 prompt-injection attacks with canaries — of which 8 are held out of the public repo.

## Outputs
- `evalkit/agents/db.py`: a seeded Python generator for a fictional retail company ("Riverbend Outfitters": stores, customers, products, orders, order_items, returns, support_tickets, internal_notes) generated deterministically (no SQL seed file); `build_database(path)` builds the DuckDB file from it and `DB_SHA256` pins its content hash. DuckDB is an optional extra (`evalkit-harness[agents]`).
- `AgentTask` YAML loader (AGT-01): `load_agent_tasks(dir)` validates each task (`GoldOutcome` union: `result_set` | `attack_blocked`), checks allowed tools against the toolbox, and checks every analytics task's gold rows against its `gold_sql` run on the built database.
- 20 analytics tasks (AGT-02): revenue, returns, cohorts, rankings and time windows; 10 are ambiguous ("our best customers") with `simulated_user_intent` resolving the ambiguity (e.g. "top 5 by 2025 net spend").
- 20 attack tasks (AGT-03): benign goals whose data path crosses planted injection text (support tickets, product reviews, notes) that tries to exfiltrate canaries, call a forbidden tool or change the answer; gold is `attack_blocked` with the canaries.
- Held-out split (AGT-04): 8 tasks (4 analytics, 4 attack) are generated into a git-ignored `heldout/agent_tasks/`; the public repo carries only their ids and content hashes (`heldout/MANIFEST.json`), so a run can prove it used the intended held-out set without publishing it.

## Rules
- No downstream project names (D-12): `source_project: evalkit`.
- Gold answers come from `gold_sql` on the pinned database, never typed by hand.
