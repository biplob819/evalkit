# D3.1 acceptance criteria

**CMP-01** `compare()` returns one `AnswerCard` per requested model, in order, with label, answer, cited sentence texts, single-judge groundedness, cost and latency; a failing model yields a card with an error instead of failing the call; replay mode makes no model calls.
**CMP-02** A PMID is fetched once from E-utilities and split into numbered sentences; neither the question nor the abstract is written to disk or logs (live calls use an in-memory store); fetch failures and empty abstracts give a clear error.
**CMP-03** Live mode runs the models in parallel and uses one judge, with per-stage timeouts so a slow stage marks its cards "timed out" instead of blocking; the cards state that live mode uses one judge.
**CMP-04** The guard refuses a wrong passcode (constant-time, before any outside call), a session over its rate limit, and any request whose upper-bound cost exceeds the per-request cap, all before any model call.
**CMP-05** The daily spend counter is reserved atomically before a request and settled after, so concurrent requests can never exceed the daily cap together; it lives in Postgres (SQLite locally) and survives restarts.
**CMP-06** Every card carries the research-only notice; advice-seeking questions are flagged (`advice`) so the page shows it prominently, and ordinary research questions are not flagged (amended by D-56 after the D3.1 verifier).
