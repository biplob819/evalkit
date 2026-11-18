# Walkthrough GIF: the screens it shows

`docs/assets/walkthrough.gif` is the walkthrough for the portfolio site. It is silent, loops,
and holds each screen for 2.5 seconds (25 seconds in all, 960 px wide). Every screen is a real
capture of a committed file, or the real output of an offline EvalKit command on a committed
config or test fixture. Nothing in it is live or paid: no API key was set, no model was called,
the demo passcode was not used, and the capture cost $0.

| # | Screen | Source |
|---|---|---|
| 1 | README: the headline block (chart, headline sentence, links) | `README.md`, between the `evalkit:headline` markers; badges left out |
| 2 | Report: run header with calibration status ("uncalibrated", the judge-not-calibrated stamp) and the headline scatter, correct-and-fully-grounded against cost with 95% intervals per judge | `docs/results/report.html` |
| 3 | Report: failure explorer, one failed answer open with its claims, each judge's verdict and the cited sentences | same file, "Failure explorer"; case ids masked |
| 4 | Terminal: dry-run cost quote for the 200-case PubMedQA benchmark, $14.13, no API calls | `uv run evalkit run configs/pubmedqa.yaml --task pubmedqa --n 200 --dry-run` |
| 5 | Terminal: release gates on a metrics-1 file, verdict PASS | `uv run evalkit gate tests/fixtures/gates/assay/metrics.json --config tests/fixtures/gates/assay/gates.yaml`; the notes are elided |
| 6 | Run comparison, per-metric change with 95% intervals and a Holm correction | the markdown from `uv run evalkit compare` on `tests/fixtures/compare/assay/old` and `new`, with `--gates tests/fixtures/gates/assay/gates.yaml --md` |
| 7 | Ledger trend chart: leakage over seven runs with warn and alarm bands and an alarm on the last run | `uv run evalkit history show --html` on the fixture ledger of `tests/test_ledger_trend.py` (its leakage chart is the golden `tests/fixtures/ledger/trend-leakage.svg`) |
| 8 | Security kit summary: splits, stopping layers and public categories for the fixture agent | `tests/fixtures/security/golden/report.md`; the per-case table is left out |
| 9 | Downstream canary: seven downstream projects' own EvalKit tests and their import audit | `docs/measurements/downstream-canary-2026-11-18.json`, shown as a table |
| 10 | Decision log, D-1 to D-9 | `docs/decisions.md` |

Captured at 1280×800 with headless Chrome and joined with ffmpeg at a 128-colour palette.
Terminal output is shown as a plain monospace page. The capture script reruns every command,
so the GIF can be rebuilt from a clean checkout:

```sh
uv run --extra anthropic --extra openai --with markdown \
    python scripts/capture_walkthrough.py --scratch /tmp/walkthrough
```

The provider SDK extras are needed only because `--dry-run` checks that they are installed; it
makes no call. Command outputs are written to the git-ignored `runs/walkthrough/` folder and
removed afterwards, and the frames stay in the scratch folder.
