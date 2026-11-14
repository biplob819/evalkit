# C3.2 acceptance criteria

**SBX-01** Each trial runs on its own read-only copy; an attempted write (`CREATE`, `INSERT`, `DROP`, `ATTACH`, `COPY`, multiple statements) is refused and recorded, and the source database is unchanged after any trial.
**SBX-02** A tampered copy (hash mismatch) yields a void trajectory with `sandbox_reset_ok=False` and no model calls.
**SBX-03** Calls to tools outside the allow-list, forbidden tools, and schema-invalid arguments are not executed and are recorded as violations on the step; results are capped and say so.
**SBX-04** The same loop drives a Claude agent and a GPT agent through scripted adapters; it stops at `final_answer` or at the step budget (recorded as over budget); malformed actions are recorded and count as steps.
**SBX-05** Every step's tool, arguments, result summary, error, tokens and latency are recorded; trajectories are cached and a rerun makes no calls.
