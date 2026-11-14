## What this changes and why

## How it was tested

- [ ] `uv run ruff check . && uv run ruff format --check . && uv run pyright && uv run pytest -q`
- [ ] New behaviour and its error cases have tests; no test makes a paid call
- [ ] Scorer or node output changed? Its version is bumped (docs/versioning.md)
- [ ] `changelog.md` has a line
- [ ] No keys, `.env` contents or private data anywhere in the diff
