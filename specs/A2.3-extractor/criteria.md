# A2.3 acceptance criteria

**EXT-01** `ClaimExtractor` behaviour is pinned: the prompt is a module constant, its hash is part of `extractor_version`, and changing the prompt changes `extractor_version` and the cache key.
**EXT-02** `ExtractClaims` sends one structured request per valid answer through the adapter for `RunConfig.extractor` at `extractor_effort`, returns a `ClaimSet` of `Claim`s with ids `<case_id>:<model>:<i>`, and repeated inputs are cache hits; errored or empty answers make no call.
**EXT-03** B11 fixtures: claims must be non-empty strings; cited numbers outside the answer's citations, duplicate claims, empty claim lists for a non-empty answer, and malformed JSON are schema errors; prose around JSON is rejected.
**EXT-04** `coverage(claim_set)` is the share of claims marked as addressing the question, 0.0 with no claims; the value is in [0, 1] and recorded in details.
