# Privacy

**EvalKit has no telemetry.** It does not phone home, collect usage statistics or send crash
reports.

## Where your data goes

EvalKit makes network requests only for these purposes:

| Purpose | Destination | When |
|---|---|---|
| Model calls (answers, claim extraction, judging) | The providers you configure: Anthropic, OpenAI, or the OpenAI-compatible endpoints you list | `evalkit run`, `evalkit agent-run`, `evalkit demo-cache` |
| Public benchmark data | Hugging Face datasets server (PubMedQA), the SciFact release on Amazon S3 | First use of `--task pubmedqa` or `--task scifact`, then cached |
| Reachability check | `api.anthropic.com`, `api.openai.com` (a TCP connection, no data sent) | `evalkit doctor` without `--offline` |
| Demo only: abstract lookup | NCBI PubMed E-utilities | When a demo visitor enters a PubMed ID |

Your questions and documents are sent to the model providers you configure, under
those providers' own terms. Nothing is sent anywhere else.

## What is stored on your machine

- `.evalkit/store/`: the cache of model calls and their results, so reruns are free.
- `runs/<run id>/`: the records, summary and report of each run.
- Your calibration labels, in the run folder.
- `~/.cache/evalkit/`: the downloaded public datasets.

Delete any of these at any time; the cache rebuilds as needed (at the cost of the calls).

## The hosted demo

The demo stores no question, abstract, PubMed ID, passcode, API key, session token or IP
address. Each live request writes one log line with its time, mode, models, latency and
cost, and a daily spend total is kept in a database so the budget survives restarts.

`tests/test_privacy.py` checks this statement against the code: only the modules named
above may open network connections.
