# D3.2 acceptance criteria

**DEM-01** `evalkit demo-cache` builds `demo/replay.json` from the sample set through `compare()`, prints a cost estimate before any call, enforces a cap, and replays from the cache on a rerun; every sample in the file renders in replay mode.
**DEM-02** The page opens in replay mode with a sample picker and makes no model call until live mode is chosen and the passcode accepted or the visitor's own keys given (D-58).
**DEM-03** Live mode takes a passcode (owner's keys) or the visitor's own API keys (D-58: no passcode, nothing held against the owner's budget, per-request cap and rate limit still apply, keys never stored or logged), a question and either a pasted abstract or a PMID; it shows the request's cost bound and today's remaining budget, and surfaces guard refusals (including a spend-store outage) as readable messages, never a traceback; with own keys the rate limit is met before any PubMed fetch.
**DEM-04** Four answer cards render with label, answer, cited sentences, groundedness, cost and latency; each fills in when its model finishes; the research-only notice is on every card (prominent for advice questions) and the page has the banner and a link to the benchmark report.
**DEM-05** Live requests are logged with only time, mode, models, latency and cost; a test feeds a request containing a unique marker through the page and finds the marker in no log line or file; the same holds for a visitor's API key.
**DEM-06** The app is deployed with all four secrets in the host's secret store and none in the repository or the browser.
