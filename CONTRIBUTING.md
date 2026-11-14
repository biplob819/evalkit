# Contributing to EvalKit

Thanks for helping. Bug reports, new example tasks, scorers and documentation fixes are all
welcome.

## Set up

```bash
git clone https://github.com/biplob819/evalkit
cd evalkit
uv sync --all-extras
uv run pytest -q
```

The test suite makes no network calls and spends nothing: every model is scripted. You do
not need API keys to contribute.

## Before you open a pull request

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest -q
```

CI runs the same checks on Python 3.11 to 3.13 on Linux, macOS and Windows.

- **Tests first.** A bug fix comes with a test that fails without it; a feature comes with
  tests for its behaviour and its error cases.
- **Never run a paid command from a test.** Use the scripted adapters in
  `tests/pipeline/fakes.py`; `evalkit run` in tests is always `--dry-run` or scripted.
- **Keys never appear in code, tests, logs or output.** `scripts/secret_scan.py` runs in CI.
- **Text files are UTF-8**, and every `open`/`read_text`/`write_text` names its encoding (a
  test enforces this, for Windows).
- **Changing what a scorer or pipeline node outputs?** Bump its version so cached results
  are not mixed (see [docs/versioning.md](docs/versioning.md)), then run
  `uv run python scripts/check_code_versions.py --update`.
- Add a line to `changelog.md`.

## Proposing a larger change

Open an issue first describing the problem and the change you have in mind, so we can agree
on the approach before you invest the time. Design decisions are recorded in
`docs/decisions.md`.

## Adding a dataset

Public datasets need a licence that allows redistribution and evaluation use, recorded in
`DATA_CARD.md` with attribution. Downloads are verified against a pinned checksum.

By contributing you agree that your work is released under the project's MIT licence, and to
follow the [code of conduct](CODE_OF_CONDUCT.md).
