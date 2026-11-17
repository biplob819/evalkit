# Check for contamination with the paraphrase probe

PubMedQA has been public since 2019, so the models you compare have probably seen it. The probe
asks 100 PQA-L questions twice: once in the original wording and once paraphrased. Both
versions use the same ids, abstracts and labels. A model that memorised the wording loses
accuracy on the paraphrase. The method is in [DATA_CARD.md](../DATA_CARD.md) (DAT-06).

## 1. Run both wordings

```bash
evalkit run evalkit.yaml --task pubmedqa-paraphrase-original --n 100 --dry-run   # estimate first
evalkit run evalkit.yaml --task pubmedqa-paraphrase-original --n 100
evalkit run evalkit.yaml --task pubmedqa-paraphrase --n 100
```

Both tasks use the `pubmedqa` prompt, labels and scorers unchanged. The original-wording task
sends the same prompts as `pubmedqa`, so any of its cases already in your store cost nothing.
Before a run starts, each load checks the vendored set against PQA-L: the file hashes, every
pubid, and a replay of the seeded draw with its replacements.

## 2. Read the report

```bash
evalkit report --paraphrase runs/r-ORIGINAL runs/r-PARAPHRASE        # add --json for one object
```

For each model in both runs you get accuracy on each wording, and the drop
(original minus paraphrase) with a paired bootstrap 95% interval (10,000 resamples, seed 7).
A pair is a probe pubid where both runs produced an answer for that model. A drop whose
interval excludes zero is flagged as a contamination signal. A rise that excludes zero is
flagged too, as a sign the set needs checking. Accuracy comes from `label_correct`, so the
report works while judging is still pending. The report is also written to
`contamination.json` in the paraphrase run folder. Any PubMedQA run that contains the probe
pubids can stand in for the original run.

**Errors never create a flag.** An errored completion (`transport`, `schema` or `refusal`)
is not scored as wrong here, unlike in a benchmark run. The report counts errors for each
model and wording, and leaves out every pair where either side errored. The interval is
computed only on pairs where both wordings produced an answer. When the excluded pairs are
more than 5% of a model's pairs, a `WARNING` line says so. Without this rule, a model that
was 100% right on the originals and hit 10 transport errors on the paraphrases would show a
false drop.

**Coverage per model.** A `coverage:` block lists, for each model: the pairs used, the probe
questions missing from each run, the pairs excluded for errors, and the error counts per
wording. The header says "all N probe questions paired for every model" only when that is
true. Otherwise it says how many probe questions both runs hold, and the per-model counts
follow. A model with no usable pair is listed as not in both runs.

**Settings must match.** For each model, the report compares what the two runs recorded:
the prompt template hash, the completion params (effort and structured output), and the
model snapshot the provider returned. Task names are not compared, because they always
differ (`pubmedqa` or `pubmedqa-paraphrase-original` against `pubmedqa-paraphrase`). The
template a task contributes is compared through its hash. Any difference is printed in a
`SETTINGS DIFFER` line above the table, and a flag for that model is labelled
`confounded`, not a contamination signal (`signal: "confounded"` in the JSON). If you have
checked that a difference does not matter (for example a snapshot you know is the same
weights), pass `--allow-different-settings`. The flag is then reported as a signal, and the
differences are still printed.

**Provisional until the owner spot-check.** The provenance record
(`src/evalkit/tasks/pubmedqa_paraphrase.provenance.json`) has a `spot_check.status`. It stays
`pending` until the owner has checked 20 kept paraphrases and every rejected one by hand. Until
then the report starts with `PROVISIONAL`. `--publish` exits 2 *before writing anything*, so a
refused publish never leaves a `contamination.json` behind. Without `--publish` the file is
written with `"status": "provisional"`. If the status is `failed`, no report is produced.

## 3. Rebuild or extend the set

```bash
python -m evalkit.paraphrase verify                                  # vendored set vs PQA-L
python -m evalkit.paraphrase build --out probe-set --dry-run         # estimate, no calls
python -m evalkit.paraphrase build --out probe-set --max-cost 2      # gpt-6.1-terra + Opus 5.5
```

The build prints its upper-bound estimate and the cap before the first call. It caches every
call in `.evalkit/store`, so a rerun, or a larger `--n`, pays only for questions it has not
seen. It stops at the cap (exit 3), and you resume it with the same command. It writes the
same three files as the vendored set, with the spot-check `pending`. It never writes into the
package's own set.

**The estimate is a strict upper bound** on what the build can reserve against the cap, and
so on what it can spend. It covers:

- every question in the first draw: its paraphrase call, plus a worst-case check whose
  prompt is sized with the longest paraphrase the builder accepts
  (`PARAPHRASE_MAX_PROMPT_TOKENS`, 1,024 estimated tokens, about 3,000 characters);
- a replacement draw for every reject, up to the reject cap (`--max-rejects`, by default a
  tenth of `--n` and at least 5). It is costed as the most expensive questions that could be
  drawn.

A paraphrase longer than the accepted maximum stops the build. One reject past the cap also
stops it (exit 2), because by then the paraphraser or the checker is failing. The replies are
cached, so you can inspect them first.

### How the builder follows gemma-grounded-health's build

The vendored set was built by `src/ggh/evals/probe.py` in gemma-grounded-health (c7d70d2).
The builder is aligned with that code:

| Setting | gemma-grounded-health | EvalKit builder |
|---|---|---|
| Paraphraser effort | `reasoning_effort="low"` | `low` (same) |
| Check effort | none sent, so Opus 5.5's default, `medium` (D-24) | `medium`, sent explicitly because EvalKit's adapters require an effort (same) |
| Check output limit | `max_tokens=200` | 200 (same) |
| Paraphrase text | first line of the stripped reply, verbatim | the same (no label stripping) |
| Unparseable check reply | `parse_verdict` raised, so the build stopped | stops the build (`ProbeError` naming the pubid); not counted as a reject |
| Empty or refused paraphrase | crashed the build | stops the build |
| "Same words" rule | `para.lower() != question.strip().lower()` | the same |
| Paraphrase output limit | **none** (provider default) | **1,024 tokens**, reasoning included |

The paraphrase output limit is the one difference kept on purpose. The cost cap needs a
finite bound on every call. If a reply hits the limit (stop reason `length` or `max_tokens`),
the build stops, because gemma's uncapped call would have returned a longer reply. So a
capped reply never enters a set. The builder also adds two safety stops that gemma's code
did not have: the reject cap and the paraphrase length limit. Neither changes which
questions are kept: a build either finishes with the method's selection or stops.

When a build stops on a reply, the round's calls are already cached and counted against the
cap. Rerunning the same command replays the same reply and stops again. To ask the model
again, use a fresh `--store`. That repays the whole build, about $0.50 at gemma's spend.
