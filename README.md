# EvalKit

[![PyPI](https://img.shields.io/pypi/v/evalkit-harness)](https://pypi.org/project/evalkit-harness/)
[![Python](https://img.shields.io/pypi/pyversions/evalkit-harness)](https://pypi.org/project/evalkit-harness/)
[![CI](https://github.com/biplob819/evalkit/actions/workflows/ci.yml/badge.svg)](https://github.com/biplob819/evalkit/actions/workflows/ci.yml)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-blue)](https://github.com/biplob819/evalkit/blob/main/LICENSE)
[![Live demo](https://img.shields.io/badge/live%20demo-Streamlit-ff4b4b)](https://evalkit-demo.streamlit.app/)

**Find out which model gets *your* task right, and whether its answers stick to the source.**

EvalKit runs your own cases across Claude and OpenAI models and reports, side by side, how often each model is correct, whether every claim it makes is supported by the sentences it cites, and what that costs, with confidence intervals and two judges from different model families.

**[Live demo](https://evalkit-demo.streamlit.app/)** · **[Benchmark report](https://biplob819.github.io/evalkit/)** · **[Eval index](https://biplob819.github.io/evalkit/index/)** · **[PyPI](https://pypi.org/project/evalkit-harness/)** · **[Quickstart](#quickstart-in-60-seconds)** · **[User guide](https://github.com/biplob819/evalkit/blob/main/docs/guide.md)** · **[Links](#links)** · **[Report a bug](https://github.com/biplob819/evalkit/issues/new/choose)**

<!-- evalkit:headline:start -->
<p align="center"><img src="https://raw.githubusercontent.com/biplob819/evalkit/main/docs/assets/headline.svg" alt="On 200 PubMedQA questions, 4 models (claude-sonnet-5-5, claude-haiku-5-5, gpt-6.1-sol, gpt-6-luna) answered from the abstract and cited its sentences; two judges from different families (claude-opus-5-5 and gpt-6.1-sol) checked every claim. The most grounded model was gpt-6.1-sol, correct and fully grounded on 57.5% (95% CI 50.5%–64.0%) by claude-opus-5-5, 46.0% (95% CI 39.0%–53.0%) by gpt-6.1-sol; it is separated from every other model after a Holm correction (gpt-6.1-sol also judged its own answers; claude-opus-5-5 agrees); the most accurate model was claude-sonnet-5-5 (77.0% correct labels); the headline also requires every claim to be supported by the sentences the answer cites. Caveats: judge not calibrated for this task (see docs/calibrate-your-task.md). Treat these numbers as provisional." width="760"></p>

On 200 PubMedQA questions, 4 models (claude-sonnet-5-5, claude-haiku-5-5, gpt-6.1-sol, gpt-6-luna) answered from the abstract and cited its sentences; two judges from different families (claude-opus-5-5 and gpt-6.1-sol) checked every claim. The most grounded model was gpt-6.1-sol, correct and fully grounded on 57.5% (95% CI 50.5%–64.0%) by claude-opus-5-5, 46.0% (95% CI 39.0%–53.0%) by gpt-6.1-sol; it is separated from every other model after a Holm correction (gpt-6.1-sol also judged its own answers; claude-opus-5-5 agrees); the most accurate model was claude-sonnet-5-5 (77.0% correct labels); the headline also requires every claim to be supported by the sentences the answer cites. Caveats: judge not calibrated for this task (see docs/calibrate-your-task.md). Treat these numbers as provisional.

[Full report](https://biplob819.github.io/evalkit/) · results in [`docs/results/`](https://github.com/biplob819/evalkit/tree/main/docs/results)

```bash
pip install 'evalkit-harness[anthropic,openai]'
evalkit init   # scaffold a project and a three-case sample task
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3 --dry-run   # estimate
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3   # needs keys in .env
```

Questions from [PubMedQA](https://pubmedqa.github.io/) (Jin et al., 2019, MIT licence); see [DATA_CARD.md](https://github.com/biplob819/evalkit/blob/main/DATA_CARD.md). Research benchmark, not medical advice.
<!-- evalkit:headline:end -->

[![Walkthrough: the headline, the report and its failure explorer, a dry-run cost quote, release gates, a run comparison, the ledger trend, the security kit, the downstream canary and the decision log](https://raw.githubusercontent.com/biplob819/evalkit/main/docs/assets/walkthrough.gif)](https://github.com/biplob819/evalkit/blob/main/docs/walkthrough.md)

## What EvalKit is

An open-source Python harness for evaluating language models, and the systems built on them, on
**your own cases**. Public leaderboards rank models on someone else's task, and a fluent answer
can still say things its source never said. EvalKit measures both on your data:

- **Accuracy:** the share of answers with the right label (yes / no / maybe, supports / refutes,
  or your own labels).
- **Groundedness:** each answer is split into claims, and every claim is checked against the
  sentences the answer cites. The headline metric is *correct and fully grounded*.
- **Citation validity, coverage, cost and latency** for every model, from the providers' own
  usage numbers, with tokens, cache hits and cost per correct answer.
- **Honest uncertainty:** 95% bootstrap intervals on every rate, paired comparisons with a Holm
  correction, and a gap is claimed only when it survives both.
- **Judges you can check:** two judges from different families (Claude and GPT) score every
  claim, their self-preference is reported, and you
  [calibrate them](https://github.com/biplob819/evalkit/blob/main/docs/calibrate-your-task.md)
  against your own hand labels.

Around the run sit the tools a team needs to keep an evaluation honest over time: release
gates, run comparison, a run history with alarm bands, drift checks, held-out discipline and an
agent security kit. None of them calls a model or needs a key.

## Who it's for

- **Teams choosing a model** for a task where answers must come from a source: support,
  policy, research, health or legal question answering, RAG.
- **Builders of agents and LLM systems** who want a CI gate that fails on a real regression,
  not on noise, and a history that shows when a metric drifts.
- **Anyone publishing eval numbers** who wants intervals, cross-family judges, a calibration
  stamp and a cost ledger behind every figure.

## Quickstart in 60 seconds

```bash
pip install 'evalkit-harness[anthropic,openai]'
evalkit init
evalkit run evalkit.yaml --task tasks/my_task.yaml --n 3 --dry-run
```

`evalkit init` scaffolds `evalkit.yaml`, a three-case sample task and `.env.example`. The dry
run prints what a run would cost and makes no API call:

```text
dry run for my-task, 3 cases → runs/r-bded23433341
generate        claude-sonnet-5-5           3 calls  $0.0105
generate        claude-haiku-5-5            3 calls  $0.0005
generate        gpt-6.1-sol                 3 calls  $0.0105
generate        gpt-6-luna                  3 calls  $0.0005
extract_claims  claude-haiku-5-5           12 calls  $0.0036
judge           claude-opus-5-5            54 calls  $0.1242
judge           gpt-6.1-sol                54 calls  $0.0621
claims per item: planning default 4.5, PubMedQA answers (D-46, measured 4.46); an expected count, not an upper bound
estimated total $0.21 (no API calls made)
```

Add your keys to `.env` (copy `.env.example`), check the setup with `evalkit doctor`, then run
the same command without `--dry-run`. The run writes `report.html` and `summary.json` to its run
folder. No keys yet? `evalkit report --example` renders a sample report offline.

The package is `evalkit-harness` on PyPI (the name `evalkit` belongs to an unrelated project);
the command and the import are both `evalkit`. Python 3.11 to 3.13 on Linux, macOS and Windows.
Next: the [user guide](https://github.com/biplob819/evalkit/blob/main/docs/guide.md) takes your
own task from cases to a gated, tracked evaluation.

## What you get

**A report per run.** Each model's *correct and fully grounded* rate, per judge, with its 95%
interval, against cost; then the models table, pairwise comparisons, judge calibration,
self-preference and a failure explorer.

| The headline chart | The failure explorer |
|---|---|
| [![Headline chart: correct and fully grounded rate per model and judge, with 95% intervals, against cost per 100 cases](https://raw.githubusercontent.com/biplob819/evalkit/main/docs/assets/screens/report-scatter.png)](https://biplob819.github.io/evalkit/) | [![Failure explorer: one failed answer with its claims, each judge's verdict and the cited sentences](https://raw.githubusercontent.com/biplob819/evalkit/main/docs/assets/screens/failure-explorer.png)](https://biplob819.github.io/evalkit/#failures) |
| Four models on 200 PubMedQA questions, two judges, cost on a log scale. | Every answer that failed for a judge, with its claims, each judge's verdict and the cited sentences. |

**Gates and comparisons with no model call.** `evalkit gate` checks release gates on a run's
summary or any project's `metrics-1` file, with exact one-sided bounds on rates.
`evalkit compare` says, per metric and label, whether a new run is better or worse, with a 95%
interval and a Holm correction.

| Release gates | Run comparison |
|---|---|
| [![evalkit gate: one row per gate and matched metric with bound, threshold, observed value, severity and result, then the verdict](https://raw.githubusercontent.com/biplob819/evalkit/main/docs/assets/screens/gate.png)](https://github.com/biplob819/evalkit/blob/main/docs/gates.md) | [![evalkit compare: per metric and label, old and new values, the change with its 95% interval, pairing and flag, then the verdict](https://raw.githubusercontent.com/biplob819/evalkit/main/docs/assets/screens/compare.png)](https://github.com/biplob819/evalkit/blob/main/docs/compare.md) |
| A metrics-1 fixture from assay-pipeline, verdict PASS. | Two fixture runs: settings that differ, each change with its interval, verdict. |

**A history and a security kit.** The run ledger judges each run against your own history and
draws a trend page; the security kit attacks your agent and reports which defence stopped each
attack.

| Ledger trend page | Security kit summary |
|---|---|
| [![Trend page: a metric over seven runs with median line, warn and alarm bands, settings markers and an alarm on the last run](https://raw.githubusercontent.com/biplob819/evalkit/main/docs/assets/screens/ledger-trend.png)](https://github.com/biplob819/evalkit/blob/main/docs/ledger.md) | [![Security kit summary: pass^k, trial success rate and blocked rate per split, stopping layers and public categories](https://raw.githubusercontent.com/biplob819/evalkit/main/docs/assets/screens/security-summary.png)](https://github.com/biplob819/evalkit/blob/main/docs/security.md) |
| Bands from at least five comparable runs; flags by shape and label, never colour alone. | Held-out attacks appear only as split-level aggregates. |

Every screenshot is a capture of a committed file or of an offline command on a committed
fixture; none needed a key or cost anything ([shot list](https://github.com/biplob819/evalkit/blob/main/docs/walkthrough.md)).

## What's in v0.4.0

| Capability | What it does | Entry point | Reference |
|---|---|---|---|
| Runs and judges | Your cases across Claude, OpenAI and OpenAI-compatible models; claims extracted; two judges from different families on every claim, on the batch APIs at half price; every call cached, so any stop resumes free | `evalkit run` | [quickstart](https://github.com/biplob819/evalkit/blob/main/docs/quickstart.md), [write a task](https://github.com/biplob819/evalkit/blob/main/docs/writing-a-task.md) |
| Calibration | Blind hand-label sheet, held-out Cohen's kappa per judge with a 95% interval, your own self-consistency, tied to the rubric hash and scored once; any two-way criterion with `--criterion` | `evalkit calibrate` | [calibrate](https://github.com/biplob819/evalkit/blob/main/docs/calibrate-your-task.md) |
| Cost estimates | A dry-run quote per step and model with its claims-per-answer assumption, a hard cost cap checked before every call, free resume | `evalkit run` with `--dry-run` | [cost](https://github.com/biplob819/evalkit/blob/main/docs/cost.md) |
| Gates | Release gates on EvalKit summaries or any `metrics-1` file: points, exact one-sided bounds, disparity between groups, warn or fail | `evalkit gate` | [gates](https://github.com/biplob819/evalkit/blob/main/docs/gates.md) |
| Compare | Per metric and label change with a paired or unpaired 95% interval, Holm correction, a settings diff, held-out rows folded | `evalkit compare` | [compare](https://github.com/biplob819/evalkit/blob/main/docs/compare.md) |
| Ledger and drift | Hash-chained run, experiment and spend ledgers; alarm bands from your own history; a trend page; a spend cap alarm; PSI per feature and Brier, ECE and reliability-bin drift | `evalkit history`, `evalkit experiment`, `evalkit spend`, `evalkit drift` | [ledger](https://github.com/biplob819/evalkit/blob/main/docs/ledger.md), [drift](https://github.com/biplob819/evalkit/blob/main/docs/drift.md) |
| Security kit | OWASP LLM Top 10 attack cases with planted canaries, k trials and pass^k, stopping-layer attribution, a model-free scripted attacker, metrics-1 output for gates | `python -m evalkit.security` | [security](https://github.com/biplob819/evalkit/blob/main/docs/security.md) |
| Held-out discipline | A manifest of hashes, score once per rubric, an exposure scan for leaked held-out text, a tamper-evident append-only log | `python -m evalkit.heldout` | [heldout](https://github.com/biplob819/evalkit/blob/main/docs/heldout.md) |
| Scorers | Label, citation validity, groundedness, coverage and per-sentence citation support; `sql_match` with opt-in shape equivalences (`percent_scale`, `pivot`, `empty_check`, `period_number`); `value_check`, which verifies "field is value" facts lines and calls prose unverifiable; your own scorers and adapters as plugins | `evalkit.scorers` | [plugins](https://github.com/biplob819/evalkit/blob/main/docs/plugins.md) |
| Contamination probe | The same PubMedQA questions reworded, original against paraphrase, to see whether accuracy rests on memorised wording | `evalkit report` with `--paraphrase` | [contamination](https://github.com/biplob819/evalkit/blob/main/docs/contamination.md) |
| Eval index | One page with every project's headline, calibration stamp, gate status and efficiency | `evalkit index` | [index](https://github.com/biplob819/evalkit/blob/main/docs/index.md) |

What is public API, and how releases follow semver: [versioning](https://github.com/biplob819/evalkit/blob/main/docs/versioning.md).

## How it works

```mermaid
flowchart LR
  A[Your cases<br/>JSONL, CSV or a built-in task] --> B[Generate<br/>each model answers and cites sentences]
  B --> C[Validate<br/>schema and citations]
  C --> D[Extract claims<br/>one fixed extractor]
  D --> E1[Judge: Claude]
  D --> E2[Judge: GPT]
  C --> F[Deterministic scores<br/>label, citations, cost, latency]
  E1 --> G[Aggregate<br/>bootstrap CIs, thresholds]
  E2 --> G
  F --> G
  G --> H[report.html<br/>summary.json]
```

- Every model call is cached by its exact inputs, so a run stopped by the cost cap, a crash or
  Ctrl-C resumes for free with the same command.
- Judges run on the providers' batch APIs at half price; `--live-judges` judges immediately at
  full price.
- A cost cap (`max_cost_usd` or `--max-cost`) is enforced before each call; the run stops with
  exit code 3 rather than overspend.
- Thresholds in `evalkit.yaml` make the exit code fail when a model misses the bar, so a run can
  gate CI.

## Guides

| Guide | What it covers |
|---|---|
| [User guide](https://github.com/biplob819/evalkit/blob/main/docs/guide.md) | Your own task end to end: cases, config, cost quote, run, report, calibration, CI gate, compare, ledger, drift |
| [Quickstart](https://github.com/biplob819/evalkit/blob/main/docs/quickstart.md) | Install, sample task, first result in under five minutes |
| [Write your own task](https://github.com/biplob819/evalkit/blob/main/docs/writing-a-task.md) | Your data as JSONL or CSV, prompts, labels, slices and thresholds |
| [Control cost](https://github.com/biplob819/evalkit/blob/main/docs/cost.md) | Estimates, the cost cap, free resume, batch versus live judging, exit codes |
| [Calibrate the judges](https://github.com/biplob819/evalkit/blob/main/docs/calibrate-your-task.md) | Hand-label claims, measure judge agreement, mark a run calibrated; any criterion |
| [Gates](https://github.com/biplob819/evalkit/blob/main/docs/gates.md) | The `metrics-1` format, gates files, exact intervals, exit codes |
| [Compare two runs](https://github.com/biplob819/evalkit/blob/main/docs/compare.md) | Per-metric changes with intervals, pairing, Holm, held-out rows |
| [Run history, experiments and spend](https://github.com/biplob819/evalkit/blob/main/docs/ledger.md) | Alarm bands against each metric's own history, a trend page, an experiment ledger with a held-out freeze guard, a spend cap alarm |
| [Drift and calibration](https://github.com/biplob819/evalkit/blob/main/docs/drift.md) | PSI per feature and Brier, ECE and reliability-bin changes against a frozen reference |
| [Keep your held-out split honest](https://github.com/biplob819/evalkit/blob/main/docs/heldout.md) | Manifest, score once, exposure scan, append-only log |
| [Agent security evals](https://github.com/biplob819/evalkit/blob/main/docs/security.md) | Attack cases, stopping layers, pass^k, scripted attacker, gates |
| [Contamination probe](https://github.com/biplob819/evalkit/blob/main/docs/contamination.md) | Original against paraphrased PubMedQA questions |
| [Scorers and adapters](https://github.com/biplob819/evalkit/blob/main/docs/plugins.md) | Add a scorer or a model adapter; `sql_match` equivalences; `value_check` facts lines; your own system |
| [Run it in CI](https://github.com/biplob819/evalkit/blob/main/examples/github-action/evalkit.yml) | A GitHub Actions workflow that estimates, runs, gates and uploads the report |
| [The eval index](https://github.com/biplob819/evalkit/blob/main/docs/index.md) | Every project side by side, built offline from snapshots |
| [Versioning](https://github.com/biplob819/evalkit/blob/main/docs/versioning.md) and [upgrading per project](https://github.com/biplob819/evalkit/blob/main/docs/downstream.md) | What is public API, semver, the 0.3 → 0.4 migration |

## Built-in tasks and your own data

| Task | Data | Labels |
|---|---|---|
| `pubmedqa` | [PubMedQA](https://pubmedqa.github.io/) expert-labelled questions with their abstracts | yes / no / maybe |
| `scifact` | [SciFact](https://github.com/allenai/scifact) scientific claims with evidence abstracts | supports / refutes / not enough info |
| your own | A JSONL or CSV file: question, context sentences, expected label | your label set |

Downloads are verified against pinned checksums; licences and sampling are in
[DATA_CARD.md](https://github.com/biplob819/evalkit/blob/main/DATA_CARD.md).

## Used by

Seven projects build on EvalKit, each pinned in
[downstream.tsv](https://github.com/biplob819/evalkit/blob/main/docs/downstream.tsv). A canary
runs each project's own EvalKit tests against every release candidate, and the
[eval index](https://biplob819.github.io/evalkit/index/) shows them side by side.

| Project | What it is | What it uses from EvalKit |
|---|---|---|
| [agentic-analytics](https://github.com/biplob819/agentic-analytics) | A ClickHouse MCP agent (Claude, GPT) against Snowflake Cortex Analyst on a 100-question analytics benchmark | `sql_match` and its shape equivalences, clustered intervals, cost estimates |
| [ai-visibility](https://github.com/biplob819/ai-visibility) | Which websites Claude and GPT cite for UK health questions, and how fresh those pages are | Two-judge support checks of whether cited pages state the claim |
| [assay-pipeline](https://github.com/biplob819/assay-pipeline) | EGFR bioactivity data read by an AI agent through a SQL guard and a hash-chained audit log, attacked with OWASP LLM Top 10 cases | `sql_match`, attack outcomes and gates; its attack suite became the security kit |
| [course-assistant](https://github.com/biplob819/course-assistant) | A course assistant that answers only from approved session materials and cites every slide | System adapter, task scorers, slices, calibration of its integrity criterion |
| [fraud-review-bedrock](https://github.com/biplob819/fraud-review-bedrock) | A fraud-review agent on Amazon Bedrock that explains each decision against a written policy (simulated data) | Faithfulness, cross-family judges, paired bootstrap with Holm, `value_check`, PSI drift |
| [gemma-grounded-health](https://github.com/biplob819/gemma-grounded-health) | A small open Gemma model fine-tuned to answer health questions only from a given abstract and cite it | The PubMedQA task, answer parsing, judge identity, the summary format |
| [respectful-offers](https://github.com/biplob819/respectful-offers) | A subscription offer policy with a contextual bandit and two respect rules, evaluated offline | A `metrics-1` export gated with `evalkit gate`, drift checks at warn severity |

How each project moves to 0.4: [the per-project guide](https://github.com/biplob819/evalkit/blob/main/docs/downstream.md).

## The live demo

**[evalkit-demo.streamlit.app](https://evalkit-demo.streamlit.app/)** shows the idea on one
question at a time.

| Replay mode | Live mode |
|---|---|
| [![Demo replay: four models answer the same question with their cited sentences checked](https://raw.githubusercontent.com/biplob819/evalkit/main/docs/assets/screenshots/demo-replay.png)](https://evalkit-demo.streamlit.app/) | [![Demo live mode: passcode, optional own API keys, question and abstract](https://raw.githubusercontent.com/biplob819/evalkit/main/docs/assets/screenshots/demo-live.png)](https://evalkit-demo.streamlit.app/) |
| Open to everyone: prepared answers to sample PubMedQA questions, no model calls. | Needs a passcode from the owner, then runs on the owner's keys (within a daily budget) or on your own keys. |

Your keys go only into that request's API clients: never saved, never logged. Live requests are
rate-limited per visitor and capped per request; only aggregate counts are logged, never the
question or the abstract. The demo's source is in
[`demo/`](https://github.com/biplob819/evalkit/tree/main/demo).

## Status: what is live, and what is not

- **Released:** v0.4.0 on [PyPI](https://pypi.org/project/evalkit-harness/) (2026-11-18), by
  trusted publishing from a tag, with CI green and a post-release install check on Linux, macOS
  and Windows.
- **Live:** the [benchmark report](https://biplob819.github.io/evalkit/) on GitHub Pages, the
  [demo](https://evalkit-demo.streamlit.app/) (replay open, live behind a passcode), and the
  [cross-project eval index](https://biplob819.github.io/evalkit/index/), which is being
  published to the same site.
- **Provisional:** the benchmark's judges are **not yet calibrated** against hand labels, so
  its report carries the "judge not calibrated for this task" stamp and its numbers are
  provisional. The hand labels are the open item; until they are done, no headline is
  published as calibrated (`evalkit report --publish` refuses).
- **Downstream:** the v0.4.0 release-candidate canary passed for three of the seven projects;
  the other four fail only known-drift checks (version-pin tests, a byte-for-byte summary
  fixture, a config-spec test), each explained in
  [the canary record](https://github.com/biplob819/evalkit/blob/main/docs/measurements/downstream-canary-2026-11-18-rc4.json)
  and fixed in that project's own build when it moves to 0.4.
- **Spend:** the 200-question benchmark cost $23.87 in API calls and the whole project $31.56,
  every dollar in the
  [spend ledger](https://github.com/biplob819/evalkit/blob/main/docs/measurements/spend-ledger.jsonl).

## Results

The first benchmark (200 PubMedQA questions, four models, two judges) is in the
[full report](https://biplob819.github.io/evalkit/), with the raw summary in
[`docs/results/`](https://github.com/biplob819/evalkit/tree/main/docs/results). How it was
built and what was decided along the way: the
[case study](https://github.com/biplob819/evalkit/blob/main/docs/case-study.md).

## Links

| | |
|---|---|
| Repository | https://github.com/biplob819/evalkit |
| Package | https://pypi.org/project/evalkit-harness/ |
| Benchmark report | https://biplob819.github.io/evalkit/ |
| Live demo | https://evalkit-demo.streamlit.app/ |
| Cross-project eval index | https://biplob819.github.io/evalkit/index/ |
| User guide | [docs/guide.md](https://github.com/biplob819/evalkit/blob/main/docs/guide.md) |
| Docs | [docs/](https://github.com/biplob819/evalkit/tree/main/docs) |
| Case study | [docs/case-study.md](https://github.com/biplob819/evalkit/blob/main/docs/case-study.md) |
| Decision log | [docs/decisions.md](https://github.com/biplob819/evalkit/blob/main/docs/decisions.md) |
| Lessons | [docs/lessons.md](https://github.com/biplob819/evalkit/blob/main/docs/lessons.md) |
| Changelog | [changelog.md](https://github.com/biplob819/evalkit/blob/main/changelog.md) |
| Product requirements and tech spec | [EvalKit PRD.md](https://github.com/biplob819/evalkit/blob/main/EvalKit%20PRD.md), [EvalKit Tech Spec.md](https://github.com/biplob819/evalkit/blob/main/EvalKit%20Tech%20Spec.md) |
| Data card | [DATA_CARD.md](https://github.com/biplob819/evalkit/blob/main/DATA_CARD.md) |
| Walkthrough shot list | [docs/walkthrough.md](https://github.com/biplob819/evalkit/blob/main/docs/walkthrough.md) |
| Contributing | [CONTRIBUTING.md](https://github.com/biplob819/evalkit/blob/main/CONTRIBUTING.md) |
| Code of conduct | [CODE_OF_CONDUCT.md](https://github.com/biplob819/evalkit/blob/main/CODE_OF_CONDUCT.md) |
| Security | [SECURITY.md](https://github.com/biplob819/evalkit/blob/main/SECURITY.md) |
| Privacy | [PRIVACY.md](https://github.com/biplob819/evalkit/blob/main/PRIVACY.md) |
| Licence | [LICENSE](https://github.com/biplob819/evalkit/blob/main/LICENSE) (MIT) |
| Issues | https://github.com/biplob819/evalkit/issues/new/choose |

Build status: `uv run python scripts/status.py` (the project was built node by node, each
verified against written criteria and an independent reviewer).

## Contributing, security and privacy

- **No telemetry.** Data goes only to the model providers you configure:
  [PRIVACY.md](https://github.com/biplob819/evalkit/blob/main/PRIVACY.md).
- **Security:** key handling and how to report a vulnerability privately:
  [SECURITY.md](https://github.com/biplob819/evalkit/blob/main/SECURITY.md).
- **Contributing:** set-up, checks and conventions in
  [CONTRIBUTING.md](https://github.com/biplob819/evalkit/blob/main/CONTRIBUTING.md); please
  follow the [code of conduct](https://github.com/biplob819/evalkit/blob/main/CODE_OF_CONDUCT.md).
  Bug reports and ideas: [open an issue](https://github.com/biplob819/evalkit/issues/new/choose).
- To work on EvalKit itself: clone the repository and run `uv sync --all-extras`.

## Credits and licence

Questions from PubMedQA (Jin et al., 2019, MIT licence) and claims from SciFact (Wadden et al.,
2020); see [DATA_CARD.md](https://github.com/biplob819/evalkit/blob/main/DATA_CARD.md). EvalKit
is a research tool, not medical advice.

MIT licence. See [LICENSE](https://github.com/biplob819/evalkit/blob/main/LICENSE).
