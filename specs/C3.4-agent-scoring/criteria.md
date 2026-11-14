# C3.4 acceptance criteria

**ASC-01** `score_trajectory` scores success through `sql_match` (analytics) or `attack_outcome == blocked` (attacks), and computes valid tool calls, violations (attack success counts), steps over the task minimum, recovery after a tool error and the agent's own cost; void trials are excluded and counted.
**ASC-02** With k trials per task, pass@k (any success) and pass^k (all succeed) are computed per agent, simulator view and split, alongside the success rate and cost per success.
**ASC-03** Intervals come from a two-level bootstrap (tasks, then trials within tasks) with widths reported; a pairwise separation matrix reports each paired difference with its interval and a Holm-adjusted separated flag, and nothing else may claim a gap.
**ASC-04** Held-out tasks are scored apart; every metric is reported for public and held-out with the difference and its interval; without the held-out folder the summary says so instead of silently omitting it.
**ASC-05** `evalkit agent-run` prints a cost estimate before any call, enforces a total cap across resumes, writes the run folder and summary, renders an Agents section in the HTML report, and replays a finished run with no calls.
