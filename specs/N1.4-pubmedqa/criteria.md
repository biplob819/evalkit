# N1.4 acceptance criteria

**DAT-01** Loader returns `Case` objects with id, question, context sentences, reference label and source; the downloaded file is cached and checksummed.
**DAT-02** Splitter output is stable across runs and handles "et al.", "e.g.", "i.e.", decimals and "p < 0.05" without breaking mid-sentence; sentences are numbered from 1.
**DAT-03** Same seed → same sample; label proportions within one case of the population proportions; the realised mix is returned.
**DAT-04** `pubmedqa.yaml` loads through `load_task_config`; the rendered prompt shows numbered sentences and never includes `long_answer`.
**DAT-05** `DATA_CARD.md` states subset, size, licence, sampling, "maybe" policy and contamination caveat (test checks each heading).
**DAT-06** The paraphrase probe spec defines how many questions, how paraphrases are produced and stored, and how the accuracy drop is reported.
