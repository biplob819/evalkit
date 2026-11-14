# Data card: PubMedQA (PQA-L) in EvalKit

## Source
PubMedQA (Jin et al., 2019), Hugging Face dataset `qiaojin/PubMedQA`, configuration `pqa_labeled`. Each item is a research question built from a PubMed article title, the article's abstract without its conclusion, and an expert yes/no/maybe answer.

## Licence
MIT. EvalKit credits the dataset here and in the README, and will publish its own hand labels on extracted claims under the same licence.

## Subset and size
PQA-L only: 1,000 expert-labelled questions (552 yes, 338 no, 110 maybe). The 61k unlabelled and 211k artificially generated subsets are not used. The loader pins the subset's checksum and row count, and refuses to run if either changes.

## Fields used
`pubid`, `question`, `context.contexts` (the abstract's sections, split into numbered sentences), `final_decision` (the reference label). `long_answer` is dropped at load time: it is the paper's conclusion and would leak the answer.

## Sampling
Runs draw a seeded sample stratified by reference label, so each label keeps its share of the 1,000 within one case. The sampler returns the realised label mix; from node A2.5 it is written to every run summary.

## "Maybe" policy
"Maybe" is a valid label, scored like the others, and reported separately, because expert agreement is lowest on it and models often avoid it.

## Contamination caveat
PubMedQA has been public since 2019 and is very likely in the training data of every model EvalKit compares. Label accuracy may therefore be inflated. EvalKit leads with groundedness and citation validity, treats accuracy as secondary, and runs the contamination probe below.

## Contamination probe
- **Questions:** exactly 100, drawn from PQA-L with the stratified sampler at seed 7 (55 yes, 34 no, 11 maybe: the population shares after largest-remainder rounding). If a paraphrase is rejected, the next question from the same label's seeded order replaces it, until 100 are accepted.
- **Paraphrases:** each question is rewritten once by `gpt-6.1-terra`, a model not in the comparison, keeping meaning, entities and numbers and changing wording and order. Prompt and model are recorded by hash.
- **Check:** Claude Opus 5.5 (the primary judge) is asked whether the paraphrase asks the same question as the original, given the abstract; only "same" is kept. The owner spot-checks 20 kept and all rejected paraphrases by hand. Rejects are logged with the reason.
- **Storage:** `src/evalkit/tasks/pubmedqa_paraphrase.jsonl`, one record per accepted question: `pubid`, `reference_label`, `original`, `paraphrase`, `paraphrase_model`, `paraphrase_prompt_hash`, `check_verdict`, `check_model`.
- **Report:** accuracy on original versus paraphrased wording for each model, with a paired bootstrap 95% interval on the difference (10,000 resamples, seed 7). A drop whose interval excludes zero is flagged as a contamination signal on the dashboard.

## What this data cannot tell you
How models behave on questions written by patients or clinicians, on full-text papers, on sources other than abstracts, or in languages other than English. PQA-L questions come from article titles, so they are cleaner and more answerable than real questions.

---

# Data card: SciFact (dev) in EvalKit

## Source
SciFact (Wadden et al., 2020, "Fact or Fiction: Verifying Scientific Claims", EMNLP), from the
Allen Institute for AI: https://github.com/allenai/scifact. EvalKit downloads the release
archive `https://scifact.s3-us-west-2.amazonaws.com/release/latest/data.tar.gz` on first use,
checks it against a pinned SHA-256, and reads only `claims_dev.jsonl` and `corpus.jsonl`, in
memory.

## Licence
Claims and evidence annotations: CC BY 4.0. Abstracts: from S2ORC, ODC-By 1.0. Both allow
redistribution and commercial use with attribution. (The Hugging Face card for
`allenai/scifact` states CC BY-NC 2.0; EvalKit follows the authors' own LICENSE.md in the
GitHub repository and downloads from their release, not from Hugging Face.) Attribution: Wadden, Lin, Lo, Wang, van
Zuylen, Cohan and Hajishirzi (2020). The test fixture in `tests/datasets/test_scifact.py` is
synthetic and contains no SciFact text.

## Subset and size
The 300 dev claims (the test split's labels are not public): 124 supported, 64
contradicted, 112 with no evidence. EvalKit maps SUPPORT to "supports", CONTRADICT to
"refutes", and no evidence to "not enough info".

## Fields used
For each claim: the claim text (shown as the question) and one abstract as the context, split
into sentences by the dataset itself. The abstract is the one carrying the evidence; for a
claim with no evidence it is the first cited abstract (only 2 such claims cite more than
one). The gold
rationale sentence numbers are kept in the cached data for later citation checks.

## Sampling
Seeded and stratified by label, like PubMedQA.

## Caveats
SciFact has been public since 2020 and is likely in models' training data. Claims were
written by experts from citation sentences, so their style differs from questions people ask.
"Not enough info" claims here come with an abstract the claim cites but that does not address
it, which is easier than open-domain retrieval. This is a research benchmark, not medical
advice.
