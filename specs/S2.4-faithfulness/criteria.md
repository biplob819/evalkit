# S2.4 acceptance criteria

**FTH-01** `faithfulness` extracts claims from each explanation with the pinned extractor, treating every evidence sentence as cited, and returns per-item results with claims and verdicts; a rerun on the same items makes no model calls.
**FTH-02** Each claim is judged blind against the evidence by the configured judges (live or batch); `score` is the share of judged claims supported per judge, `fully_faithful` requires every claim judged and supported, and an explanation with no claims is not faithful.
**FTH-03** A fixture of explanations with known faithful and unfaithful claims scores as expected with scripted replies, and `scripts/faithfulness_check.py` runs the same fixture live with an estimate first.
