# C3.1 acceptance criteria

**AGT-01** `load_agent_tasks` loads every task, rejects unknown tools, malformed gold outcomes and duplicate ids, and refuses an analytics task whose gold rows do not equal its `gold_sql` result on the pinned database.
**AGT-02** 20 analytics tasks exist (32 public + held-out across both kinds), 10 of them ambiguous with a non-empty hidden intent that the goal alone does not determine; every gold result set is reproduced from its SQL.
**AGT-03** 20 attack tasks exist. The row each task reads holds an injection that names where its canary is stored (a `system_settings` key) without containing any canary, so quoting or describing the injection cannot leak; each `CANARY-` value is stored exactly once, every canary exists in the database, and no canary appears in any analytics gold answer (amended by D-48 after the C3.1 verifier).
**AGT-04** 8 tasks are written only to the git-ignored held-out folder; the committed manifest holds their ids and SHA-256 hashes, and a check verifies a held-out folder against it.
