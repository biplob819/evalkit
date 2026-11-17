# B5.12 — Per-sentence citation support

**Wave 5. Depends on:** A2.4 (judges), A2.5 (aggregate). **Asked by:** gemma-grounded-health (its OWNER D-44). Decision: D-65 (built next after B5.1).

## Goal
Show, per cited sentence, whether it actually supports the claim that cites it and what role it plays in the source, so a model that cites the study's aim sentence instead of its result is visible in one number.

## Why
gemma-grounded-health's main run (r-f0f3b432a413, EvalKit 06591f8, PubMedQA n=1000, dual judges) has the fine-tuned model at citation validity 1.000 but groundedness 0.519 (Opus) against base 0.863. 303 of its 525 unsupported claims cite sentence [1], usually the aim or question sentence. EvalKit cannot show this today: `citation_valid` checks only that cited numbers exist, and a claim verdict is against the cited set as a whole. A set-level judgement also hides an aim sentence cited beside a supporting result sentence.

## Interface

### Opt-in (CIT-01)
- A task turns it on by listing `citation_support` in `scorers`. It joins the benchmark set computed by the pipeline (`BENCHMARK_SCORERS`), so it is not looked up as a plugin. Tasks that do not list it behave and serialise exactly as before.
- After claim judging, a new **citation-judge node** runs once per claim per judge, for claims that passed the B3 precheck (cited sentences exist). The judge sees the claim and each cited sentence on its own, redacted and blinded as in A2.4, and returns for each cited sentence `{n, supports: bool, role}` with `role` in `aim` (question, objective or hypothesis), `background`, `method`, `result`, `conclusion`.
- Its rubric, schema and code version form `CITATION_PROMPT_HASH`, separate from `JUDGE_PROMPT_HASH`. Benchmark judge prompts, `JUDGE_PROMPT_HASH` and every existing cache key are unchanged (tested against values pinned before the change).
- Batch by default and live with `--live-judges`, through the same machinery as claim judging (budget holds, resume, `custom_id` mapping, one retry on a cut-off reply). The cost estimate includes it before any call. A rerun that adds `citation_support` to a task re-uses cached answers, claims and verdicts and pays only for citation calls.
- Parsing: every cited sentence must be answered exactly once, numbers must be the cited ones, `role` must be one of the five; anything else is a schema failure handled like a claim-judge schema failure (one retry, then recorded as unjudged and counted).

### Records and metrics (CIT-02)
- `Record.citation_support`: per judge, a list of `{claim_id, n, supports, role}`. Omitted when empty, so records of tasks without the scorer keep their bytes.
- Per answer and judge: the cited set S is the distinct sentence numbers cited by its judged claims; a sentence **supports** if it supports at least one of the answer's claims; its **role** is the most common role that judge gave it in that answer (ties broken in the order aim, background, method, conclusion, result, so an aim reading is never hidden).
- **Citation precision** per model and judge: Σ supporting / Σ |S| over answers with at least one judged citation.
- **Aim-citation rate** per model and judge: share of answers with at least one judged citation that cite a sentence whose role is `aim` and that supports none of the answer's claims.
- Both carry 95% bootstrap intervals over cases on the run's seeded resamples (as the other model metrics), go in `ModelSummary.citation_precision` and `ModelSummary.aim_citation_rate` (per-judge maps, omitted when empty), appear in the report's model table and in slices, and work for built-in and local JSONL tasks.
- While citation judging is pending, both are absent and the summary is "judging pending" (exit 4), as for claim judging.

### Tests from the asking project (CIT-03)
- A vendored record from gemma's run, pqa-10808977, with hand-written citation-judge replies (no API call; they test the pipeline's handling, not a real judge's reading): its cited sentence comes out `role=aim, supports=false`, and the answer counts in the aim-citation rate.
- A fixture where a claim cites an aim sentence and a result sentence and only the result supports it: precision is 1/2, the aim sentence counts as non-supporting, and the answer counts in the aim-citation rate, although the claim itself is supported.

## Out of scope
- Using the citation verdicts to change claim verdicts or the headline.
- Roles from dataset section labels (PQA-L's labels are dropped at load time and the dataset hash is pinned).
- Re-scoring gemma's run (its owner approves that spend on its side; EvalKit prints the estimate).
