# D3.2 — Demo page and replay

**Wave 3. Depends on:** D3.1, B2.2. **Unblocks:** release notes, portfolio links.

## Goal
A one-screen web demo (US8, US9): pick a sample question and see four scored answer cards instantly (replay, no API calls), or, with the passcode, ask your own question about a pasted abstract or a PubMed ID and watch four live cards fill in. Safe to share: replay is the default, live mode is guarded and capped, and no question text is stored.

## Outputs
- `evalkit demo-cache` (DEM-01): runs `compare()` live over a curated sample set (questions and abstracts from the published benchmark run, by case id) and writes `demo/replay.json` (cards keyed by `replay_key`), with a cost estimate first and a cap; re-running replays from the cache.
- `demo/app.py`, a Streamlit page (DEM-02 to DEM-04):
  - Replay by default: a sample picker, the abstract, and four cards from `replay.json`.
  - Live mode: the owner's keys behind the passcode, or the visitor's own API keys (D-58); question, a pasted abstract or a PMID, the per-request cost line and today's remaining budget; cards fill in as each model finishes.
  - Every card: label, answer, cited sentences, groundedness from one judge with the "live mode uses one judge" note, cost and latency; the research-only notice on every card, prominent when the question is advice-seeking (D-56); a banner and a link to the full benchmark report.
- Aggregate-only logging (DEM-05): one line per live request with UTC time, mode, models, latency and cost; never the question, abstract, PMID, passcode, API keys, session token or IP.
- Deployment (DEM-06): Streamlit Community Cloud from this repository, with `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `EVALKIT_DEMO_PASSCODE` and `EVALKIT_DEMO_DATABASE_URL` in the host's secret store; needs the owner's accounts. `demo/requirements.txt` pins the third-party packages (it takes precedence over the root `uv.lock` on Community Cloud); the app imports evalkit from `src/`.
