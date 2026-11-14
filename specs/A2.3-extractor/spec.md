# A2.3 — Claim extractor

**Wave 2. Depends on:** A2.1. **Unblocks:** A2.4, S2.4, D3.1.

## Goal
Split each valid answer into atomic claims once, with one pinned extractor, so both judges score exactly the same claims (D-3) and coverage can be measured (C5, D-31).

## Outputs
- `evalkit/pipeline/extract.py` — `EXTRACTOR_PROMPT` (versioned), `extraction_schema()`, `parse_claims()`, `ExtractInput`, `ClaimSet`, the `ExtractClaims` node (EXT-01, EXT-02), and `coverage()` (EXT-04).
- `extractor_version` = `ext<code_version>-<sha256(prompt)[:8]>`, stamped on every `Claim`.

## Rules (B11)
- Every claim is one checkable statement taken from the answer text; the extractor may not add, merge or drop content.
- A claim's `cited` numbers must be a subset of the answer's own citations; the extractor assigns which cited sentence supports which claim. Numbers outside that set are a schema error (the extractor invented a citation).
- Claim ids are `<case_id>:<model>:<i>`, 0-based, in answer order.
- Answers with an error or no answer text yield an empty ClaimSet without calling the extractor.
- An unparseable extraction yields `ClaimSet(error="schema")` with no claims; it is never retried with hints.
- The extractor runs at `RunConfig.extractor_effort` (low, D-28) with structured output.
