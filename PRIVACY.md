# Privacy

**EvalKit has no telemetry.** It does not phone home, collect usage statistics or send crash
reports.

## Where your data goes

EvalKit makes network requests only for these purposes:

| Purpose | Destination | When |
|---|---|---|
| Model calls (answers, claim extraction, judging) | The providers you configure: Anthropic, OpenAI, or the OpenAI-compatible endpoints you list | `evalkit run`, `evalkit agent-run`, `evalkit demo-cache` |
| Calibration labelling page | Nothing outbound: a local server on `127.0.0.1` that only your browser reaches | `evalkit calibrate label RUN_DIR` (also with `--criterion`; `--context` files are read locally and shown only on that page) |
| Public benchmark data | Hugging Face datasets server (PubMedQA), the SciFact release on Amazon S3 | First use of `--task pubmedqa` or `--task scifact`, then cached |
| Reachability check | `api.anthropic.com`, `api.openai.com` (a TCP connection, no data sent) | `evalkit doctor` without `--offline` |
| Demo only: model calls | Anthropic and OpenAI, behind the owner's passcode, on the owner's keys or the visitor's own keys | Each live request |
| Demo only: abstract lookup | NCBI PubMed E-utilities | When a demo visitor enters a PubMed ID |
| Demo only: daily spend total | The demo's Postgres database (one number per day, nothing about requests) | Each live request |

Your questions and documents are sent to the model providers you configure, under
those providers' own terms. Nothing is sent anywhere else.

## What is stored on your machine

- `.evalkit/store/`: the cache of model calls and their results, so reruns are free.
- `runs/<run id>/`: the records, summary and report of each run.
- Your calibration labels, in the run folder.
- `~/.cache/evalkit/` (or `$EVALKIT_CACHE/evalkit`, or `$XDG_CACHE_HOME/evalkit`): the
  downloaded public datasets.

Delete any of these at any time; the cache rebuilds as needed (at the cost of the calls).

## The hosted demo

The demo stores no question, abstract, PubMed ID, passcode, API key, session token or IP
address. Each live request writes one log line with its time, mode, models, latency and
cost, and a daily spend total is kept in a database so the budget survives restarts.
Streamlit's usage statistics are switched off (`.streamlit/config.toml`). The hosting
platform (Streamlit Community Cloud) keeps its own server logs under its own terms.

`tests/test_release.py` checks this statement against the code: only the modules named
above may import networking libraries.

The held-out checks (`python -m evalkit.heldout`) run local `git` commands that read your
repository (`ls-files`, `show`, `rev-list`, `rev-parse`, `merge-base`); they never fetch or
push.
