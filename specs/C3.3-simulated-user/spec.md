# C3.3 — Simulated user

**Wave 3. Depends on:** C3.2. **Unblocks:** C3.4.

## Goal
On ambiguous analytics tasks the agent may ask the user a clarifying question. A simulated user answers from the task's hidden intent only, so the agent can be scored on whether it asked and used the answer, not on guessing. Two simulators from different families (Claude Haiku 5.5 and GPT Luna) are run so results can be reported per simulator (FR22) and a family-matched simulator cannot quietly favour its own agent.

## Outputs (`evalkit/agents/simuser.py`)
- `SimulatedUser(model, task, ...)` returning the `UserFn` the sandbox calls for `ask_user`, plus its `name` (`<model>@<prompt version>`) recorded on every trajectory (SIM-03).
- Each reply is one structured-output call (`{"reply": str}`), a cached executor node keyed by simulator model, intent, goal, the trial's question history, trial and seed (SIM-01). Models do not accept sampling parameters (D-22), so "seeded" means: the seed and trial are in the cache key, and a rerun replays the same replies.
- The simulator sees only the goal, the hidden intent and the agent's questions: never the database, the gold answer or the gold SQL (B9).
- Answer guard (SIM-02): a deterministic check after every reply. A reply that states a value from the gold answer that neither the goal nor the intent contains is withheld and replaced by a fixed sentence asking the agent to work it out from the data; the step records that the guard fired.
- Live check (`scripts/simuser_check.py`, B9): 10 scripted questions (plain clarifications, out-of-scope questions, and attempts to extract the answer) against both simulators; every reply must be non-empty, must resolve the ambiguity when asked about it, and must never state the answer. Its cost is reported.
