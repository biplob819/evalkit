# A2.4 — Dual-judge batch

**Wave 2. Depends on:** A2.3, N1.2. **Unblocks:** A2.5, A2.6, S2.4, D3.1.

## Goal
Both judges (Claude Opus 5.5 and GPT Sol, OWNER D-4) score every extracted claim against only the sentences it cites, through each provider's Batch API, blind to which model wrote the answer; verdicts are cached per claim and a resumed run never pays twice.

## Outputs (`evalkit/pipeline/judge.py`)
- `JUDGE_RUBRIC` (v1) and `judge_prompt_hash` (JDG-01); `render_judge_prompt` that contains only the claim and its cited sentences, never model or provider names (JDG-02).
- `precheck(claim, case)` — a claim with no citations or a citation outside the context is `unsupported` with no model call (B3).
- `judge_claims(...)` — per judge: cached verdicts reused; claims already in a submitted batch (per-claim store meta) wait for that batch; the rest go into one new batch with opaque `custom_id`s, polled within `max_wait`, collected by `custom_id`, parsed and cached per claim (JDG-03, JDG-04). Returns `JudgingResult(verdicts, status, n_errors, cost_usd, batch_ids)`; `status="pending"` when the batch has not finished within `max_wait` (NFR1).
- `parse_verdict` — `{verdict, evidence, quote, rationale}`; evidence must be a subset of the claim's citations; supported needs evidence, unsupported has none, and a quote must appear in the cited sentences; a parse failure gets one live retry (D-35), then excludes the claim from that judge and is counted (JDG-05, Tech Spec §2.4).
- `groundedness(verdicts)` per judge: share of judged claims supported; `fully_grounded` only when every claim was judged and supported (JDG-06).
- `PLANTED_CLAIMS` fixture (10 true-but-absent claims) and `planted_claim_check(verdicts)` (JDG-07; the live run happens with calibration once keys exist).
- `select_judges(config, available_providers)` → judges and `judge_mode`: dual only with two judges from different providers, else single (JDG-08, D-10).
- Executor gains `reserve` / `settle` and `hold_batch` / `settle_batch`: an open batch's estimate stays held against the cost cap until it is collected, and is released exactly once.

## Rules
- Judges run at `judge_effort` (medium, D-34), structured output, no sampling params, no fallbacks (D-23).
- A batch-level failure (expired/cancelled) leaves affected claims unjudged and counted, never "unsupported".
- Claim and cited sentences are redacted whole-word for model and provider names before the judge sees them (B4, D-35).
- A batch item that fails transiently loses its batch meta, so the next run resubmits it.
