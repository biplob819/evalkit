# C3.2 — Sandbox and trial runner

**Wave 3. Depends on:** C3.1, N1.2. **Unblocks:** C3.3, C3.4.

## Goal
Run one agent on one task in a sealed, resettable environment and record everything it did: every tool call, its arguments, the result it saw, tokens, latency and cost — so trajectory scorers (C3.4) can judge behaviour, not just the final words.

## Outputs (`evalkit/agents/sandbox.py`, `evalkit/agents/loop.py`)
- Sandbox (SBX-01): each trial gets its own copy of the pinned database, opened read-only; nothing a trial does can reach another trial or the source file.
- Reset check (SBX-02): before a trial the copy's canonical content hash must equal `DB_SHA256`; on mismatch the trial is not run and is recorded with `sandbox_reset_ok=False` (void, excluded from scores and counted).
- Tools (SBX-03): `list_tables`, `describe_table(table)`, `run_sql(query)` (one read-only statement; SELECT/WITH only; row cap with a truncation note), `ask_user(question)` (answered by the simulated user, C3.3), `final_answer(answer, columns?, rows?)`. Arguments are validated against JSON schemas. A tool outside the task's allow-list, or arguments that fail validation, is not executed and is recorded as a violation on the step.
- Agent loop (SBX-04): one provider-neutral loop for the Claude and GPT agents: each turn the model returns a structured JSON action `{tool, arguments, reason}` (structured outputs on both providers, D-24); the tool result is appended and the loop continues until `final_answer` or the task's `step_budget` (over budget = failure).
- Trajectory recording (SBX-05): a `Trajectory` per trial (steps with tool, arguments, result summary, error, tokens, latency; final answer and result rows; cost; seed; reset flag), cached through the executor so re-running a trial is free.
