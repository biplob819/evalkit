# A2.2 acceptance criteria

**DET-01** `label_correct` is 1.0 for a matching label (trim/case-insensitive), 0.0 otherwise, and 0.0 for errored or answerless completions or unlabelled cases.
**DET-02** `citation_valid` returns the share of distinct citations inside 1..len(context); duplicates count once; no citations or no answer gives 0.0; details list the invalid numbers.
**DET-03** `cost` returns `cost_usd` and `latency` returns `latency_ms` for every completion, errored or not.
**DET-04** Every scorer satisfies the `Scorer` protocol, is registered by name and importable as `from evalkit.scorers import label_correct, citation_valid, cost, latency, get_scorer`.
**DET-05** A scorer registered under the `evalkit.scorers` entry-point group is discovered; a broken or name-clashing plugin warns and leaves the built-ins working.
