# EvalKit: which model gets your task right, and sticks to its source

Teams pick a model from leaderboards built on someone else's task, then grade it with an LLM
judge nobody has checked. I built EvalKit, an open-source Python harness that runs your own
cases across Claude and OpenAI models and has judges from two model families check every claim
in each answer against the sentences it cites. Its first
benchmark put 200 <!-- docs/results/run.json: n --> PubMedQA questions to four <!-- docs/results/run.json: models (4 entries) --> models. GPT-6.1 Sol was the most grounded:
correct and fully grounded on 57.5% <!-- docs/results/summary.json: models[2].headline.claude-opus-5-5.point --> of answers by the Claude Opus 5.5 judge and 46.0% <!-- docs/results/summary.json: models[2].headline.gpt-6.1-sol.point --> by the
GPT-6.1 Sol judge (which was rating its own answers), and separated from every other model after a Holm correction under both <!-- docs/results/summary.json: comparisons[7,9,11,13,15,17].separated = true (metric headline) -->.
Claude Sonnet 5.5 was the most accurate, 77.0% <!-- docs/results/summary.json: models[0].accuracy.point --> correct labels, yet correct and fully
grounded on only 38.5% <!-- docs/results/summary.json: models[0].headline.claude-opus-5-5.point --> by the Claude judge. The benchmark cost $23.87 <!-- docs/results/run.json: spent_usd (23.8710259) -->, the whole
project $31.56 <!-- docs/measurements/spend-ledger.jsonl: sum of spent_usd over the 22 legacy lines (31.557859); line 23 is the $0 seal (D-73) --> in API calls. Both judges are calibrated against held-out reference labels that a model, Claude Opus 5.5, made from a fixed prompt (D-81) <!-- docs/results/summary.json: calibration_status = "calibrated" -->: the Sol judge at
κ=0.60 <!-- docs/results/summary.json: judges[gpt-6.1-sol].kappa.point (0.598) -->, the Opus judge at κ=0.40 <!-- docs/results/summary.json: judges[claude-opus-5-5].kappa (0.397, lower bound 0.263) -->, below the 0.4 certify bar on its lower bound
and lenient (it passed 52 of 79 claims the labels marked unsupported <!-- docs/results/summary.json: judges[claude-opus-5-5].confusion = [[151, 1], [52, 27]] -->), so its 57.5% likely
overstates. The Opus judge is also the model that made the labels, so its agreement is not independent; the Sol judge's is a check across model families. EvalKit is on PyPI (v0.1.0 and v0.2.0 <!-- docs/measurements/release-v0.1.0-2026-11-15.json; docs/measurements/release-v0.2.0-2026-11-16.json -->), with a public report, a live demo, and
seven <!-- docs/downstream.tsv: 7 project rows --> projects that build on it.

## My role

Product owner and decision-maker. I owned the PRD and technical spec and made the calls
recorded as OWNER entries in the decision log: both judges on every claim of the full run
(D-4); quality before cost, with a larger budget for stronger judging (D-34); a gap between
models counts only if it survives a Holm correction (D-36); withdrawing the first headline and
re-judging the benchmark after a 40-claim <!-- docs/measurements/spend-ledger.jsonl: line 11 (kind rejudge_sample).claims --> check showed the judges had been given the
extractor's guessed citations, not the sentences each answer actually cited (D-52); a passcode
on every live demo request (D-59); and Wave 5, which makes EvalKit the shared eval harness for
my other projects (D-61 to D-63). I added the API keys, approved the spend from the $0.24 <!-- docs/measurements/spend-ledger.jsonl: line 1 (kind smoke).spent_usd (0.241784) -->
smoke run (D-30) to the $8.48 <!-- docs/measurements/spend-ledger.jsonl: line 18 (kind benchmark_rejudge_200).spent_usd (8.482221) --> re-judge, and entered the passcode for the demo's live
deployment check. I chose to have a model make the calibration labels rather than label them myself, since I am not a domain expert (D-81). The 20-answer relabel that measures the labeller's consistency is the open item.

Claude Code wrote the code, tests and docs under a lean adaptation of the Kova node harness
(D-8). The build is a dependency graph of nodes, and a node starts only when everything it
depends on is green. For each node Claude wrote failing tests for every acceptance criterion
first, then the code, then had to pass 12 <!-- scripts/node_converge.py: module docstring, "Gates" list 1-12; docs/graph/catalog.tsv: row HAR-05 --> gates, among them lint, types, golden, judge and
safety tests, a clean-wheel install and a check that changed source came with changed tests.
A fresh-context verifier agent, which never sees the builder's reasoning, then tried to
disprove each criterion; verifiers found real faults, such as summary rules loose enough to
publish a "calibrated" stamp that was not true (D-39). Wave 5 ran as parallel lanes, one
builder per node in its own git worktree, with a lead agent merging one lane at a time (D-66).

## Key decisions

1. **Two judges from different families check every claim (OWNER D-4).** Claude Opus 5.5 and
   GPT-6.1 Sol rule on every claim of the full run, on one claim set from a single fixed
   extractor (D-3), with model names redacted (D-35). Why: a judge can favour its own family,
   and that only shows when both judge every answer. Here both rated GPT answers better
   grounded than Claude answers (Opus 88.8% <!-- docs/results/summary.json: self_preference.claude-opus-5-5.openai.point --> against 81.4% <!-- docs/results/summary.json: self_preference.claude-opus-5-5.anthropic.point -->, Sol 80.9% <!-- docs/results/summary.json: self_preference.gpt-6.1-sol.openai.point --> against 72.0% <!-- docs/results/summary.json: self_preference.gpt-6.1-sol.anthropic.point -->
   mean groundedness), so the ranking is not one judge favouring its own. *Trade-off:* every
   claim is paid for twice, and the report carries two headline numbers that disagree instead
   of one clean figure.
2. **Report judge agreement only on held-out labels (D-5, D-41, D-43).** Labels are
   split into a tuning half and a held-out half; agreement (Cohen's kappa, with an interval
   that resamples whole answers) comes from the held-out half only, and a run is stamped
   calibrated only when every judge has been scored that way for the task under the current
   rubric. Why: a kappa measured on the labels used to tune the rubric is training accuracy,
   not an error rate. *Trade-off:* the published benchmark stayed marked uncalibrated and provisional until the
   labels were done (2026-11-19); EvalKit would not call it anything else. The labels then
   showed one judge below the certify bar, and the headline says so (D-80). A model made them
   (Claude Opus 5.5, D-81), so the reports name it and flag the Opus judge as not independent.
3. **EvalKit grows with the projects built on it, behind a canary (OWNER D-61 to D-63).** Before
   a release, a canary runs each downstream project's own offline EvalKit tests against the
   current code; an internal name that two or more projects use joins the public API; and every
   change made for a project ships with that project's failing case as an EvalKit test. On the
   first baseline five projects passed all their tests (1,303 <!-- docs/measurements/downstream-canary-2026-11-17.json: sum of projects.{agentic-analytics,ai-visibility,assay-pipeline,course-assistant,fraud-review-bedrock}.counts.passed = 109+18+60+151+965 -->), gemma-grounded-health showed
   one <!-- docs/measurements/downstream-canary-2026-11-17.json: projects.gemma-grounded-health.counts.failed; status = "known-drift" --> known drift that cannot affect it until it upgrades from its v0.1.0 <!-- docs/downstream.tsv: gemma-grounded-health pin @06591f8...; docs/measurements/release-v0.1.0-2026-11-15.json: commit (same hash) --> pin, and respectful-offers
   imports nothing, so has none to run; the canary cost $0 <!-- docs/measurements/downstream-canary-2026-11-17.json: spend_usd -->. Why: projects were rebuilding the
   same gates, comparisons and labelling checks, and without a canary a release could break
   them silently. *Trade-off:* a wider surface to maintain, with projects' requests queueing until a release ships them (v0.3.0 <!-- docs/measurements/release-v0.3.0-2026-11-17.json: tag, version -->
   shipped the first batch on 17 November); a capability with one user and no likely second
   stays in its own project (D-63).

**Links:** [repository](https://github.com/biplob819/evalkit) ·
[report](https://biplob819.github.io/evalkit/) ·
[live demo](https://evalkit-demo.streamlit.app/) ·
[PyPI](https://pypi.org/project/evalkit-harness/) ·
[decision log](https://github.com/biplob819/evalkit/blob/main/docs/decisions.md)
