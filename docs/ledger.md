# Run history, experiments and spend

EvalKit keeps three ledgers per project and tells you, per metric and model, when a new run
or this month's spend has moved outside what your own history says is normal. Gates judge one
run against fixed bars and [compare](compare.md) judges two runs; the ledger judges a run
against its own history. Drift and calibration checks for a new batch of data are in
[drift.md](drift.md).

| Ledger | Default path | Commands |
|---|---|---|
| Run history | `docs/measurements/runs.jsonl` | `history init`, `add`, `check`, `show`, `reset`, `verify` |
| Experiments | `docs/measurements/experiments.jsonl` | `experiment open`, `close`, `look`, `show` |
| Spend | `docs/measurements/spend-ledger.jsonl` | `spend add`, `check` |

Nothing here makes a model call or a network call, and no API key is needed. Every command
takes `--json` and prints one JSON object.

## Every line is chained

Every line EvalKit writes is canonical JSON (sorted keys, compact, ASCII) whose `prev` is the
sha256 of the line before it, written under an exclusive file lock in one write. This is the
same writer as the [held-out log](heldout.md). A deleted bad run would silently re-centre
every band, a deleted experiment would hide a spent held-out look, and a deleted spend line
would silence the cap alarm, so an edited, dropped or reordered line breaks the chain.

Every entry has `kind`, `schema` (`ledger-1`), `at` (UTC, seconds) and `project`; a ledger
holds one project. Free-text fields (`note`, `hypothesis`, `change`, `reason`) are at most 500
characters. Set `SOURCE_DATE_EPOCH` to fix `at` when you need byte-identical ledgers.

```
evalkit history verify
evalkit history verify docs/measurements/runs.jsonl --base BEFORE_SHA --default-branch main
```

`history verify` checks each ledger's chain and, unless you pass `--chain-only`, that it is
append-only in git: the push base, every parent of HEAD and the default branch tip, exactly
as `python -m evalkit.heldout verify-log` does. It exits 1 on any problem.

A hash chain shows an edited, dropped or reordered line, but not lines cut from its end: what
is left is still a valid chain. **The defence against a cut tail is the git append-only
check**, so `verify` reports for every ledger whether it ran (`append-only: checked against N
revision(s)` or `not checked`; `append_only` in the JSON). Run it with `--base` (CI: the push's
before) or `--rev`; `--chain-only`, or no revision to compare with, leaves a cut tail unseen.
One cut is caught by the chain check itself: a spend ledger cut back to a legacy prefix that
EvalKit has already sealed (see Legacy lines below).

**A missing ledger is an error.** `history check`, `history show` and `report --history` exit
2 when the ledger path does not exist, naming it, so a mistyped path never reads as an empty
history. `evalkit history init --ledger docs/measurements/runs.jsonl` creates an empty ledger (an existing one is
left as it is), and the first `history add` creates one too.

## Run history

```
evalkit history add runs/r-20261117 --gates gates.yaml
evalkit history check results/metrics.json
evalkit history show --html history.html --md history.md
```

RUN is anything `evalkit gate` reads: a [metrics-1](gates.md) file, an EvalKit run folder or
its `summary.json` (with its efficiency block), or a folder holding `metrics.json`. `add`
appends one `run` entry and judges the run against the history before it; `check` only
judges. The entry stores the run's metrics inline, so the history stands on its own when run
folders are not committed:

| Field | What it holds |
|---|---|
| `run_id`, `source` | the run, and `metrics-1` or `evalkit-summary` |
| `summary_sha256` | sha256 of the input file's canonical JSON (sorted keys, compact, ASCII); a run is recorded once, so the same file pretty-printed or minified is refused (exit 2) |
| `config_hash`, `code_version`, `snapshot`, `evalkit` | from the run's provenance (null when unrecorded) |
| `recorded_with`, `settings_sha256` | the EvalKit that wrote the line; a hash of the run's settings (seed and EvalKit version excluded) |
| `simulated`, `status` | the run's flag; `complete` or `stopped` |
| `price_dates` | the price-table date per model (EFF-03) |
| `metrics`, `heldout_rows_folded` | the metrics-1 rows; held-out rows below split level dropped and counted |

- A run with judging pending is refused unrecorded (exit 4).
- A run stopped at its cost cap is recorded with `status: stopped`, kept out of every band,
  and exits 3.
- A held-out row (split `heldout`, the gates file's `heldout_splits`, or `--heldout-split`)
  keeps its figures only when its labels are within split, model and judge, as compare
  folds them. With `--manifest`, a line that names a held-out case is refused (exit 2); see
  [Held-out rules](#held-out-rules).
- An efficiency row's `price_date` label moves to `price_dates`, so a new price table keeps
  the row comparable and shows up as a hint instead of a new series.

### Alarm bands

Each (metric, labels) of a new run is judged against its comparable history: the last 20
complete runs of the same project, metric, labels, unit and `simulated` flag, after the latest
`band_reset` for it.

- **Learning:** with fewer than 5 such runs the row is `learning`: shown and charted, never
  flagged.
- **Centre** c = the median of the history. **Spread** ŝ = max(1.4826 × MAD, the median SE of
  the history, the SE of the new run). A run's SE is its interval width / 3.92; for a rate with
  n and no interval, √(p(1−p)/n) with p clamped to [0.5/n, 1 − 0.5/n]; else 0.
- **warn** beyond 2ŝ from the centre; **alarm** beyond 3ŝ (exit 1). With `--gates`, a gated
  metric alarms only on the side that moves toward failing its gate (`>=`: below, `<=`: above,
  `==`: either); a move the other way is `improved`.
- **Degenerate:** ŝ = 0 (an identical history without intervals): any other value warns and
  never alarms. True/false metrics have no band: a value other than the history's most common
  one warns.

Why median and MAD: a mean and SD are pulled toward the regression they should catch, and one
past outlier inflates the SD; the median and MAD tolerate two bad runs in five, and 1.4826 makes
MAD estimate the SD, so 2 and 3 spreads keep their usual meaning (about 4.6% and 0.27% false
flags per row for a stable normal process; bands monitor, gates decide, so there is no Holm
correction here). The SE floor stops a noisy run from alarming against a history that happens
to be tight. After a deliberate change, restart the band at once:

```
evalkit history reset --metric cost_per_case --labels model=claude-sonnet-5-5 --reason "new price table"
```

### Efficiency variation (EFF-03)

The standard efficiency names (`cost_per_case`, `cache_hit_share`, `latency_p95`, token means
per case, ...) get bands per model like any metric. `history show` adds a spread table per
(model, figure): runs, median, 1.4826 × MAD, coefficient of variation (spread / |median|), min,
max, latest and status. A moved row carries a hint, which never clears its flag:

- `price_date changed` on a cost row when the model's price date differs from the previous
  run's (a price-table change, not a regression in the system);
- `cache regression` on a drop in `cache_hit_share`;
- the percentile on a latency row (`p95 latency`).

### The trend page

`evalkit history show --html FILE` writes one page: the latest band of every metric, the
efficiency spread table, and one chart per (metric, labels) with its data table. Each chart
shows the values with their interval whiskers, each run's band (median line, warn band,
alarm band) from the history before that run, `learning` runs without a band, flagged runs by
shape and a text label (never colour alone), settings changes and band resets as vertical
markers, and a `<title>` and `<desc>`. A PSI panel per feature shows the [drift
checks](drift.md) recorded in the history, with lines at 0.1 and 0.25. The bytes repeat for
the same ledger.

`evalkit report RUN_DIR --history docs/measurements/runs.jsonl` adds the same charts, for the
metrics that run reports, as a Trend section of its report.

## Experiments and the freeze guard

```
evalkit experiment open --hypothesis "tighter grounding raises accuracy" --change "bar 0.7 to 0.8" --metric accuracy --labels model=claude-sonnet-5-5 --direction up --baseline runs/r-base
evalkit experiment look E1 --candidate runs/r-tuned --manifest heldout/MANIFEST.json --log docs/measurements/tuning-log.jsonl
evalkit experiment close E1 --before runs/r-base --after runs/r-tuned --gates gates.yaml --heldout runs/r-tuned-heldout
evalkit experiment show
```

- `open` declares the hypothesis, the change, one primary row (metric and labels) and the
  direction that would support it, before any result exists. Ids are `E1`, `E2`, ... in
  ledger order.
- `close` runs [compare](compare.md) on before and after and records its whole output
  (compare-1, held-out rows already folded) and a verdict: `supported` when the primary row is
  flagged in the declared direction, `refuted` when flagged the other way, `inconclusive`
  otherwise (including `too_few_cases` and `degenerate` rows). Its exit code is compare's (0,
  or 1 for a fail-gate regression); the entry is written for both.
- `look` freezes the candidate and reserves its one look at the held-out split **before** the
  split is scored. The look is a `heldout_scored` line in the project's
  [held-out log](heldout.md) with rubric `look:<candidate sha256>` over every case of the
  split, so the log's score-once rule refuses a second look for the same candidate: exit 1,
  nothing written. The experiment ledger gets the candidate hash, manifest hash, split, case
  count and the log line's sha256, never case hashes.
- The candidate hash is `candidate_sha256` from the candidate's `settings.json` when present
  (a fix hash, a challenger hash); otherwise the sha256 of every recorded setting except
  `seed`, `evalkit` and `results_format`, so a re-seed or an upgrade does not buy a second
  look. Run ids, times and paths are never part of it. An EvalKit run's `config_hash` is left
  out too, because it covers the seed; each model's call settings from its records
  (`effort`, `structured_output`) are hashed in its place, with the code version, prompt
  hash, model ids, judges, judge prompt hash and extractor version, and the run's `systems`
  (alias to system adapter) and `git_sha` where its run.json records them. Set-like settings
  (`models`, `judges`, `systems`, whether joined text or a settings.json list) are sorted
  first, so the same judges listed in another order are the same candidate. The code version
  is the records' `git_sha`, which is `unknown` when `EVALKIT_GIT_SHA` was not set: then every
  code version reads the same, and a code change alone does not make a new candidate (set
  `EVALKIT_GIT_SHA`, or record a `candidate_sha256`). Run-config knobs the records do not
  state (`max_output_tokens`, endpoints) are not seen: record a `candidate_sha256` when they
  matter. A project's own `config_hash` (metrics-1) is kept: if
  it covers the seed, record `candidate_sha256`. A candidate that records nothing identifying
  is refused (exit 2).
- `--candidate` names a run (folder, summary.json or metrics-1 file) or a settings file. The
  `settings.json` inside a run folder is the same candidate as the folder. A settings file on
  its own (not in a run folder) identifies a candidate only by its `candidate_sha256`;
  without one it is refused (exit 2), so copying a run's settings elsewhere buys no look.
- `close --heldout RESULT` stores the candidate's held-out result as split-level aggregates
  only, and only when RESULT's candidate hash matches a look reserved in this experiment and
  no result was recorded for it.
- `show` lists every experiment and the looks spent per split, across candidates.

The guard stops a second recorded look. Scoring the held-out set outside EvalKit without
`look` shows up only as a held-out result with no reserved look, which `close` refuses.

## Spend ledger and the cap alarm

```
evalkit spend add --kind rejudge --usd 8.48 --run-id r-20261117
evalkit spend check --cap 150 --alarm-fraction 0.9
evalkit spend check --cap 25 --window total --add-estimate 6.10
evalkit spend check --spend-ledger runs/ledger.jsonl --cap 150 --alarm-fraction 0.9 --map spent_usd=cost_usd
```

A `spend` line holds `spend_kind`, `spent_usd` (rounded to 6 decimals), and optionally
`run_id`, `model`, `n_cases`, `note` and `detail`. `smoke.record_spend` (EvalKit's own scripts)
writes through the same chained writer with its signature and running-total return unchanged.

`spend check` sums the current UTC window (`--window month`, the default; `day`; or `total`)
plus `--add-estimate`, against `--cap`:

| Status | When | Exit |
|---|---|---|
| `ok` | below alarm fraction × cap | 0 |
| `alarm` | at or above alarm fraction × cap (default 0.8); printed and in the JSON | 0: it warns about money, it does not fail quality |
| `cap` | spend reached the cap, or spend plus the estimate would exceed it | 3, so a script can refuse to start a run |

Bad input (an unreadable ledger, a broken chain, a line without a numeric amount) exits 2.

**Other projects' ledgers** are read unchanged through `--map FIELD=SOURCE`: course-assistant's
per-call lines with `--map spent_usd=cost_usd`, assay-pipeline's hand-written lines with
`--map spent_usd=usd at=date` (a bare date is a UTC date). An unchained file is reported
`unchained`, not an error. `spend add` never appends to one: that line would seal a legacy
prefix EvalKit has not recorded (below), so it exits 2; write new spend to a chained ledger.

**Legacy lines (OWNER D-73).** A spend ledger may start with lines written before chaining.
They are kept byte for byte, because published dollar figures are cited from them. The first
chained line names the last legacy line's sha256 as `prev` and records `sealed: {lines,
sha256}` over all of them, so editing any legacy line breaks the seal. EvalKit's own ledger has
22 such lines, sealed by a $0 `ledger_seal` line.

Cutting away the whole chained part, the seal included, would leave a file that looks like a
legacy file not yet sealed. So the seals EvalKit has made are recorded in its code
(`evalkit.ledger._io.KNOWN_SEALS`: line count and sha256 of the prefix, and a short sha256 of
each legacy line). A seal is valid only when it records exactly a recorded prefix, so a
shorter or edited legacy history sealed again fails. A file that holds a known sealed prefix
with no chained line after it, or some of its legacy lines but not the whole prefix (cut to
21 lines, its first line dropped), is a broken chain: `verify` reports it, `spend check`
exits 2 and `spend add` refuses to append, which would re-seal a shorter history. Lines cut from the end of the chained part leave a valid chain; the git append-only
check catches those (see `history verify` above).

## One kind per ledger

A ledger's first line declares its kind: a run ledger holds `run` and `band_reset` lines, an
experiment ledger `experiment_*` lines, a spend ledger `spend` lines (and may start with
unchained legacy lines); a chained line of another schema is a B5.14 held-out log. While a
ledger is empty its file name declares it (the word `run`/`runs`, `experiment(s)` or `spend`,
as in the defaults `runs.jsonl`, `experiments.jsonl` and `spend-ledger.jsonl`); a name with
none or several of them declares nothing. A writer refuses a ledger of another kind (exit 2,
nothing written): `spend add` into a run ledger, `experiment open` into a run ledger,
`history add` or `history reset` into a spend ledger.

## Exit codes

Precedence 2 > 3 > 4 > 1 > 0 wherever more than one applies, as for `evalkit gate`.

| Code | Meaning |
|---|---|
| 0 | pass; warns are printed and in the JSON |
| 1 | an alarm (history, drift); a fail-gate regression (experiment close); a refused second look (experiment look); a broken chain or append-only problem (history verify) |
| 2 | bad input: an unreadable or invalid file, a run already recorded, an edited reference, a held-out id in a line |
| 3 | the cost cap (spend check; a run stopped at its cap in history add or check) |
| 4 | judging pending (history add, not recorded; history check) |

## Held-out rules

No held-out case id, id hash or text is written to any ledger line, report, chart, JSON output
or error. Run entries fold held-out rows to split level; experiment entries store compare's
folded output and look metadata only; a look's case hashes live only in the held-out log. With
`--manifest`, every writer (`history add`, `history reset --reason`, `experiment open` and
`close`, `spend add --note`) checks the line it is about to append and refuses it (exit 2)
when it names a held-out case id, or a held-out `id_sha256` or `content_sha256` (the whole hash
or a prefix of at least 12 hex characters, the manifest id, in any letter case). The manifest
holds hashes only, so to refuse held-out case **text** in free-text fields (`note`,
`hypothesis`, `change`, `reason`), also pass the held-out case files with `--cases SPLIT=PATH`
(`history add`, `history reset`, `experiment open`, `experiment close`, `spend add`). They are
checked against the manifest first, then the line is read with B5.14's exposure-scan
normalisation (NFKC; invisible characters such as zero-width spaces, joiners, soft hyphens and
BOMs removed, and also read as spaces; casefolded; common Cyrillic, Greek and Armenian
homoglyphs folded to the ASCII letters they resemble; runs of punctuation and spaces as one
space): a whole case string of 25 or more characters, or any 10 consecutive words of one, is
refused. An id is matched as written and in the same folded form, so a fullwidth,
zero-width-split, homoglyph or upper-case spelling of a held-out id is refused too. The held-out
exposure scan covers the committed ledgers in CI.

## From Python

```python
from evalkit import ledger

res = ledger.record_run("docs/measurements/runs.jsonl", "runs/r-20261117", gates="gates.yaml")
print(res.exit_code, [b.as_dict() for b in res.bands if b.flagged])
status = ledger.check_spend("docs/measurements/spend-ledger.jsonl", cap=150, alarm_fraction=0.9)
```

`evalkit.ledger` also has `check_run`, `history`, `bands`, `band`, `reset_band`,
`open_experiment`, `close_experiment`, `reserve_look`, `candidate_hash`, `list_experiments`,
`record_spend`, `trend_svg`, `psi_svg`, `history_html` and `verify_ledger`
([versioning.md](versioning.md)).
