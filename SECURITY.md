# Security

## Reporting a vulnerability

Please report security problems privately through GitHub's
[private vulnerability reporting](https://github.com/biplob819/evalkit/security/advisories/new)
for this repository, not in a public issue. Include what you found, how to reproduce it, and
the version you tested. You will get an acknowledgement within a week; once a fix is
released, the advisory is published with credit to you unless you prefer otherwise.

Supported versions: the latest release.

## How EvalKit handles your API keys

- Keys are read from environment variables or a local `.env` file (`ANTHROPIC_API_KEY`,
  `OPENAI_API_KEY`). `evalkit init` adds `.env` to `.gitignore`.
- Keys are sent only to the provider they belong to, by the provider's official SDK.
- EvalKit never prints, logs or writes a key value. `evalkit doctor` reports which keys are
  set by name only. Cached results and run folders never contain keys.
- Error messages from providers are not echoed where they could quote part of a key.

## The demo

The hosted demo keeps its keys in the host's secret store, never in the repository or the
browser. A visitor's own keys are used only for that visitor's requests: held in server
memory while the tab is open, never saved or logged. Live requests are capped per request
and per day, and the log holds only time, mode, models, latency and cost.

## What to keep out of a public repository

Your `.env`, the `runs/` and `.evalkit/` folders if your data is private, and any held-out
labels you use for calibration.
