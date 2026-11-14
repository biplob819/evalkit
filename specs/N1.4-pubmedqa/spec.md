# N1.4 — PubMedQA task and data card

**Wave 1. Depends on:** N1.1. **Unblocks:** A2.1, A2.2.

## Goal
The flagship task, defined the same way a user would define theirs: a loader, numbered context, a seeded stratified sample, a YAML task file and an honest data card.

## Outputs
- `evalkit/datasets/pubmedqa.py` — loads PQA-L (1,000 expert-labelled questions) from the Hugging Face parquet/JSON, cached locally (DAT-01).
- `evalkit/text/sentences.py` — deterministic sentence splitter over the abstract contexts, producing 1-based numbered sentences (DAT-02).
- `evalkit/sampling.py` — seeded sampler stratified by reference label, with the realised label mix reported (DAT-03).
- `evalkit/tasks/pubmedqa.yaml` — prompt template, output schema, labels yes/no/maybe, scorers, default thresholds (DAT-04).
- `DATA_CARD.md` — source, licence (MIT), subset, size, sampling, "maybe" policy, contamination caveat, what it can't tell you (DAT-05).
- `evalkit/tasks/pubmedqa_paraphrase.jsonl` generator spec for the contamination probe (DAT-06).

## Design rules
- Tests never download: a 12-question fixture in `tests/fixtures/pubmedqa_mini.json` mirrors the real schema.
- The splitter is rule-based (no model, no network) and tested on abbreviations ("et al.", "e.g.", "p < 0.05", decimals).
- The prompt never contains the PubMedQA `long_answer` (it is the paper's conclusion and would leak the answer).
