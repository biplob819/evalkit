# S2.4 acceptance criteria

**FTH-01** `faithfulness` extracts claims from each explanation with the pinned faithfulness extractor (no citation list, D-60), treating every evidence sentence as cited, and returns per-item results with claims and verdicts; a rerun on the same items makes no model calls.
**FTH-02** Each claim is judged blind against the evidence by the configured judges (live or batch); `score` is the share of judged claims supported per judge, `fully_faithful` requires every claim judged and supported, and an explanation with no claims is not faithful.
**FTH-03** A fixture of explanations with known faithful and unfaithful claims scores as expected with scripted replies, and `scripts/faithfulness_check.py` runs the same fixture live with an estimate first.
**FTH-04** (D-60) The faithfulness extractor is not asked to cite and its schema has no `cited` field, so its output does not grow with the evidence; a reply cut off at the output limit is retried once at a higher limit; the benchmark extractor's node, version, prompt and cache keys are unchanged.
**FTH-05** (D-60) With `question_words=True` (default) a claim may use words from the question without failing B11's adding check; words in neither the explanation nor the question still fail it, and coverage is measured against the explanation only; `question_words=False` applies the benchmark's B11.
