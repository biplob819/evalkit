# The eval index

The eval index is one page that shows EvalKit and every registered project side by side
([downstream.tsv](downstream.tsv), D-61, D-62). For each project it shows:

- the headline metric with its interval;
- the calibration stamp;
- the gate status;
- the EvalKit version and the run date;
- per model, quality next to the efficiency block, ordered by cost per correct (D-67).

The page is built in two separate steps, so the build never calls a model or the network:

```
evalkit index snapshot --config configs/index.yaml
evalkit index build --config configs/index.yaml
```

1. **`evalkit index snapshot`** is the only step that may reach a remote. For each registered
   project it runs `git fetch --depth 1` from `https://github.com/<repo>.git` (or from a local
   repository, `--source PROJECT=PATH`), at the registry branch or `--commit PROJECT=SHA`. It
   then copies the files named below into `docs/index/snapshots/<project>/`, together with a
   `snapshot.json` that records the commit, the source, the time and each file's sha256.
   EvalKit's own row is read from the current repository at `HEAD`. A fetch that fails is
   recorded as `status: missing` with a fixed reason (`git fetch failed (exit N)`), never git's
   own output. The step reads only these paths:
   - the summary (the registry's `summary` cell, or the config's `summary`, or the file the
     config's `pointer` leads to; the pointer file itself is read, never copied);
   - `run.json` beside an EvalKit `summary.json`;
   - the config's `gates` and `experiments` files;
   - the config's `lessons` path, checked for existence only and never copied;
   - `pyproject.toml` and `uv.lock`, read only for the EvalKit requirement and the version the
     lock resolved (recorded in `snapshot.json` as `evalkit_declared` and `evalkit_locked`,
     credentials in a URL hidden), never copied.

   Absolute paths, `..` and `.env` files are refused before any git call.
2. **`evalkit index build`** reads the registry, the config and the snapshot folder, and nothing
   else. It writes `docs/index/index.html` and `docs/index/index.json`. The output carries no
   build time, so the same snapshots give the same bytes.

Exit codes: 0 when done (missing projects are listed on the page); 1 with `build --require-all`
when any project is missing; 2 for an unreadable registry or config, a `--source`/`--commit` for
an unknown project, or an output folder that cannot be written. The Pages workflow publishes
`docs/index/` at `/index/` when it exists.

Only `docs/index/index.html` and `docs/index/index.json` are committed. They hold aggregates
only. The snapshot folder holds each project's whole summary file, which may carry more than
the index shows, so `docs/index/snapshots/` is ignored by git. `index.json` records the commit
each project was read at, so the snapshot can be taken again with `--commit PROJECT=SHA`.

## What each row shows

The summary is a [`metrics-1`](gates.md) file or an EvalKit `summary.json`. The strict
run-folder marker decides which one it is; another project's own `summary.json` format is
neither.

- **Headline:** the config's `headline` selector. It must match exactly one row: its metric,
  and exactly its labels (`*` matches any value). The row is shown with its value, interval and
  method. A share of exactly 0 or 1 reads "0 of n, below X% at 95%". The index never picks a
  headline itself: without a selector the row says `not designated`, and a selector that
  matches no row or several rows shows that problem.
- **Calibration:** for an EvalKit summary, `calibrated` with each judge's held-out kappa, or
  `uncalibrated`. For a metrics-1 file, the config's `calibration` row (`stated: ...`), or
  `not stated`.
- **Gates:** the snapshot's gates file, checked through `evalkit.gates.evaluate` (exit codes and
  held-out folding as in [gates.md](gates.md)). An EvalKit summary without a gates file uses
  `evalkit gate`'s default: every model passes its thresholds. A metrics-1 file without one
  says `not gated`.
- **EvalKit version:** the version that made the run, only as the run records it (metrics-1
  `provenance.evalkit`); otherwise `not recorded in the run`. The repository's version at the
  commit is not the build that made an older run, so it is never shown as the run's version.
- **EvalKit pin:** the `evalkit-harness` requirement as the project's own `pyproject.toml`
  states it at the commit (its dependencies, optional dependencies and dependency groups),
  with the version its `uv.lock` resolved. A registry cell that disagrees is flagged
  (`the registry's pin (…) differs`), never shown as the pin. The registry cell is shown only
  when the pyproject states no EvalKit requirement, labelled as such. EvalKit's own row shows
  its repository's version at the commit, labelled `the repository at the commit`.
- **Show only what the source states:** a run mode, price date or `simulated` flag the file
  does not record reads `not recorded` (or is left out of `index.json` as null). A summary
  written before 0.3 records no run mode, an EvalKit summary has no `simulated` field, and
  metrics-1's default `false` is not a statement; a metrics-1 headline is `final` only when its
  file states `simulated: false`.
- **Run date, run id, commit:** the commit links the summary file on GitHub at that commit. A
  `simulated` run carries a SIMULATED badge (D-63), along with EvalKit's own stamps (single
  judge, uncalibrated, judging pending, missing verdicts, stopped at the cost cap).

## Missing projects

Every registered project and EvalKit appear on the page, in registry order. A project is shown
as `missing`, with the reason and no figure from anywhere else, when any of these holds:

- no summary is registered;
- no snapshot was taken;
- `snapshot.json` is unreadable;
- the fetch failed;
- the summary file is absent;
- the file changed since the snapshot (its sha256 differs);
- the file is not UTF-8 or not JSON;
- it is another project's own format (with the run-folder reason);
- the metrics-1 file or the summary is invalid (the first three problems are shown, never
  values);
- any unexpected error occurs (`unexpected <Type>`).

A broken gates, `run.json` or experiments file shows its own reason without hiding the summary.

## Efficiency per model

For each value of the `model` label, the page shows:

- the config's `quality` row, with `model` added to its labels;
- the standard efficiency names (`EFFICIENCY_NAMES`): cost per correct and per case, tokens
  in/out/reasoning/cached, cache-hit share, latency p50/p95/p99, error and retry rate, system
  cost and eval overhead, each with its interval where the file has one;
- mode and price date.

`efficiency_labels` narrows the rows (for example `{mode: live}`). A summary written before 0.3
has no efficiency block: its point cost per correct, cost per 100 ÷ 100 and latency p50/p95
stand in, marked `from summary (no interval)`. A name that matches several rows for one model
shows `ambiguous (N rows)`, never one of them.

Cost per correct is the comparison metric. Models are ordered by it, with unknown values last.
The lowest is marked, or all of them when they tie. The page says `clear` only when the lowest
model's interval lies below every other model's; otherwise it says `intervals overlap` or
`no interval to compare`.

## index.json for the portfolio

`index.json` (`schema_version: index-1`) holds every field above. Each project also carries a
`story` in the portfolio site's shapes
([specs/B5.5-eval-index/inputs.md](../specs/B5.5-eval-index/inputs.md)):

- `headline` `{value, label, note}`: the note holds the interval, run id, commit and EvalKit
  version.
- `evals` `{name, data, method, result, status, url}`: the headline first, then each gate row.
  Statuses: `pass`/`fail`; a warn row is `provisional`; an error row, or a run whose judging is
  pending or stopped at its cost cap, is `pending`; the headline is `final` only when the judge
  is calibrated (EvalKit) or the run is not simulated (metrics-1); a simulated `pass` is
  `provisional`.
- `improvements` `{metric, before, after, change, url}`: each `experiment_closed` entry of the
  project's experiment ledger ([ledger.md](ledger.md)), with the change and its interval.
- `learnings` `{lesson, now, url}`: a link to the project's lessons file at the commit. Lesson
  text from another repository is never copied.
- `monitoring` `{metric, kind, target, current, how}`: one per gate row. `kind` comes from the
  config's `kinds`; otherwise it is `north-star` for the headline metric, `cost` for money,
  `guardrail` for a count gated `<=`/`==`, and `quality` for anything else.

## Safety

- Held-out rows appear only at split level (`split`, `model`, `judge`). A selector that points
  at a held-out row below split level is refused, and gate rows are folded by `evalkit.gates`.
- metrics-1 refuses case and member ids, both as label names and as id-shaped values.
- Every value from a project's file is escaped and limited to 64 characters (an interval method
  to 128). Anthropic, OpenAI, AWS, GitHub and PyPI key shapes are shown as `<hidden>`.
- A label value shaped like an id in the index (a prefix, a hyphen or underscore, then 8 or
  more hex characters or 3 or more digits: `a-0123456789abcdef`, `case-0042`) is refused: a
  figure labelled with one shows `a label value is shaped like an id: not shown`, and a gate
  row shows the value as `<hidden>`.
- A reason never quotes the source: a duplicate key reads `a duplicate key in a JSON object`,
  and quoted keys and values in a validation problem read `'…'`.
- A direction the label already states (`… lower is better`) is not repeated beside the value.
- Answers, claims and lesson text are never read into the index.
- A file that cannot be read shows up as its project's own `missing` row; it never stops the
  page from rendering.

## The config

[`configs/index.yaml`](../configs/index.yaml) (`schema_version: index-config-1`) names, per
project, the optional keys `summary`, `pointer`, `own_format`, `headline`, `headline_pending`,
`comparison`, `quality`, `calibration`, `efficiency_labels`, `gates`, `experiments`, `lessons` and `kinds`,
plus `heldout_splits` for all projects (`heldout` always counts). Unknown keys, and projects
that are not registered, are refused. Every headline there is the one the project's own session
confirmed (file, selector and label as it sent them); a project or a part with no confirmation
has no selector, so its row says `not designated`. A model's quality cell without a `quality`
selector says `not designated` too.

`headline_pending` is for a project with no headline yet: its own words on when one will be
designated (1 to 120 characters), shown after `not designated`. course-assistant's
`headline_pending: until E2.5` makes its row read `not designated until E2.5`. It is refused
beside a `headline`.

A selector (`headline`, `comparison`, `quality`, `calibration`) also takes:

- `label`: how the index names it (EvalKit's own text, up to 300 characters);
- `direction: lower | higher`: shown beside the value as `lower is better` or
  `higher is better`, and in the portfolio note;
- `decimals: 0..6`: fixed decimals for the value and its interval (a rate as a percentage);
- `companion`: a second selector of the same kind, shown beside each value under its own
  `label` (for example a strict score beside the headline score);
- `caption`: the project's own words on the figure, shown beside it.

A `quality` selector whose labels name a `model` designates that model's quality only. The
other models keep their efficiency rows, and their quality cell says `not designated`.

`comparison` is an optional second row shown under the headline (and as a second `evals` entry
in `index.json`).

A metrics-1 or EvalKit selector names a row by `metric` and exactly its labels, so a two-arm row
is named like any other: `{metric: diff.incremental_retained_per_1000_ntd, labels: {a: bandit,
b: rule_following_baseline, variant: central}}`.

### A pointer to the current results

`pointer: {path: docs/results/LATEST, file: metrics.json}` (instead of `summary`) is for a
project whose published run changes with each release. At the snapshot's commit, the pointer
file must hold exactly one relative path, to a folder or to a file inside one, relative to the
pointer's own folder. The summary is `file` in that folder. The snapshot records the pointer
(path, the path it named, sha256) and the summary path it resolved to. A pointer that is
missing, not UTF-8, holds more than one line, names `..`, an absolute path or a `.env` file, or
names nothing at the commit makes the project `missing` with that reason.

### A project's own summary format

`own_format` reads a summary that is neither metrics-1 nor EvalKit's `summary.json`. The file
must be strict JSON (duplicate keys refused). Only the config's path selectors read it:

```yaml
own_format:
  run_id: $.run_id                 # one string, else not shown
  run_at: $.finished_at            # one ISO 8601 time with a zone, else not shown
  simulated: {path: $.data_label, equals: simulated}
headline:
  path: $.leakage                  # a number, or an object with point (lo, hi, k, n optional)
  unit: rate                       # metrics-1 units; default ratio
  ci_method: exact 95% upper bound # how the project made the interval, as it states it
  direction: lower
```

`simulated` is true when the value equals `equals`, false when the file holds another value,
and not stated when the path selects nothing: it is never invented. Such a project has no
metrics-1 rows, so its gates are `not gated`, its calibration `not stated` and it has no
per-model efficiency; `quality`, `calibration`, `efficiency_labels` and `gates` are refused
for it. The selected value goes through metrics-1's checks (a rate in [0, 1], k = value × n,
the interval around the value, no id-shaped labels), so a figure that fails them is shown as
that problem.

### The path selector

```
path    := "$" step*
step    := "." field                    field access on an object
         | "[?" cond ("&&" cond)* "]"   keep the list items whose fields all equal the values
cond    := field "==" literal
literal := "string" | number | true | false | null | []
field   := [A-Za-z_][A-Za-z0-9_]*
```

That is the whole language, parsed by hand and evaluated over plain JSON values: no `eval`, no
`exec`, no wildcards, indexes, recursion or other operators, and at most 200 characters.
Equality follows JSON's types: `true` is not `1`, `"1"` is not `1`, `null` matches only a
field that is present and null, and `[]` only an empty list. A `.field` step on a list
selects nothing; filter first. Examples from the confirmed headlines:

- `$.leakage`: one object.
- `$.share[?kind == "presence" && publisher == "nhs.uk" && model == "gpt-6.1-sol" && run == 1 && intent == null].share`:
  one row of a list.
- `$.backends[?fixes == []].accuracy` with `per: backend`: per-label fan-out. One figure per
  item the last filter kept, labelled by that item's `backend`, in file order. A companion
  with the same `per` is matched to each row by that label.

A selector must select exactly one value (with `per`: exactly one per label value). Zero
values show `matches no value`; several show `matches N values, not one value` (or `not one
per backend value`). The index never picks one of them.
