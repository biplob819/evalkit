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
The pairs are the probe pubids that both runs answered. A drop whose interval excludes zero is
flagged as a contamination signal. A rise that excludes zero is flagged too, as a sign the set
needs checking. Accuracy comes from `label_correct`, so the report works while judging is
still pending. The report is also written to `contamination.json` in the paraphrase run folder.
Any PubMedQA run that contains the probe pubids can stand in for the original run.

**Provisional until the owner spot-check.** The provenance record
(`src/evalkit/tasks/pubmedqa_paraphrase.provenance.json`) has a `spot_check.status`. It stays
`pending` until the owner has checked 20 kept paraphrases and every rejected one by hand. Until
then the report starts with `PROVISIONAL` and `--publish` exits 2. If the status is `failed`,
no report is produced.

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
