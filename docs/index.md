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
   - the summary (the registry's `summary` cell, or the config's `summary`);
   - `run.json` beside an EvalKit `summary.json`;
   - the config's `gates` and `experiments` files;
   - the config's `lessons` path, checked for existence only and never copied.

   Absolute paths, `..` and `.env` files are refused before any git call.
2. **`evalkit index build`** reads the registry, the config and the snapshot folder, and nothing
   else. It writes `docs/index/index.html` and `docs/index/index.json`. The output carries no
   build time, so the same snapshots give the same bytes.

Exit codes: 0 when done (missing projects are listed on the page); 1 with `build --require-all`
when any project is missing; 2 for an unreadable registry or config, a `--source`/`--commit` for
an unknown project, or an output folder that cannot be written. The Pages workflow publishes
`docs/index/` at `/index/` when it exists.

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
- **EvalKit version:** metrics-1 `provenance.evalkit`, or EvalKit's own `pyproject.toml` at the
  commit. Otherwise `not recorded in the run`, shown with the registry's pin.
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
- Every value from a project's file is escaped and limited to 64 characters. Anthropic, OpenAI,
  AWS, GitHub and PyPI key shapes are shown as `<hidden>`.
- Answers, claims and lesson text are never read into the index.
- A file that cannot be read shows up as its project's own `missing` row; it never stops the
  page from rendering.

## The config

[`configs/index.yaml`](../configs/index.yaml) (`schema_version: index-config-1`) names, per
project, the optional keys `summary`, `headline`, `quality`, `calibration`,
`efficiency_labels`, `gates`, `experiments`, `lessons` and `kinds`, plus `heldout_splits` for
all projects (`heldout` always counts). Unknown keys, and projects that are not registered, are
refused. The headline choices there are the owner's to confirm.
