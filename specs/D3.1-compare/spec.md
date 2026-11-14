# D3.1 — `compare()` and the live guard

**Wave 3. Depends on:** A2.2, A2.4. **Unblocks:** D3.2.

## Goal
One function behind the demo: given a health-research question and an abstract (pasted, or fetched by PMID), return one answer card per model with its label, answer, cited sentences and how grounded it is, within a latency and cost budget, and never store the question or the abstract. Live mode is protected by a passcode, a per-session rate limit, a per-request cost cap and a daily spend cap that survives restarts.

## Outputs (`evalkit/demo/`)
- `compare(question, source, models, mode)` (CMP-01): `source` is `Abstract(sentences)` or `PMID(id)`; returns `AnswerCard`s (model, label, answer, cited sentences with text, groundedness from one judge, cost, latency, error). Replay mode reads cards cached by `evalkit demo-cache` (D3.2) and makes no calls.
- PMID fetch (CMP-02): PubMed E-utilities `efetch`, abstract split into numbered sentences with the dataset splitter; held in memory only. Live calls use a throwaway in-memory store, so nothing about the question or abstract reaches disk.
- Live path (CMP-03): the models answer in parallel; extraction plus one fast judge (live, no batch); each stage has a timeout, a stage that misses its budget leaves the card with an explicit "timed out" note rather than blocking the page. The card says that live mode uses one judge.
- Guard (CMP-04): passcode (constant-time compare), per-session token bucket, per-request cap from the §9 cost formula (n = 1, one judge, no batch discount); a request whose estimate exceeds the cap is refused before any call.
- Daily spend (CMP-05): a Postgres table (owner choice, D-53) holding one row per UTC day; reserve the estimate with a single atomic `UPDATE ... WHERE spent + :est <= :cap RETURNING`, settle to the actual cost afterwards; a local SQLite fallback for development. Connection string from `EVALKIT_DEMO_DATABASE_URL`.
- Research-only notice (CMP-06, B7): advice-seeking questions (about personal treatment, doses, what "I" should do) get the research-only notice on every card.
