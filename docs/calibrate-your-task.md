# Calibrate the judges for your task

EvalKit's groundedness numbers come from two LLM judges. Until you have checked them against your own labels, every report carries the stamp **"judge not calibrated for this task"**. This guide removes that stamp honestly. It takes about 1 to 2 hours of labelling for about 300 claims.

## What calibration measures

You label claims from 100 answers yourself. EvalKit then compares each judge with you on the 50 **held-out** answers you did not use for tuning, and reports:

- **Cohen's kappa** per judge with a 95% interval. The interval resamples whole answers, because claims from one answer are not independent.
- Kappa per generator family (did a judge go easy on its own family?).
- Raw agreement and the confusion matrix.
- The **extraction error rate**: claims you flagged as misstating the answer.
- Your own **self-consistency**: you relabel 20 answers blind, and EvalKit reports kappa between your two passes. If you disagree with yourself often, no judge can be expected to agree with you.

The result is tied to the exact judge rubric (`judge_prompt_hash`). If the rubric changes, the stamp returns until you label a fresh held-out set.

## Steps

1. **Run the benchmark once** with both judges (`evalkit run`) so there are answers, claims and verdicts to compare. Export needs at least 100 answers that made claims, so evaluate enough cases (for example 30 cases with four models).
2. **Export the sample**: `evalkit calibrate export RUN_DIR`. This picks 100 answers stratified by model and reference label, splits them 50 tuning and 50 held-out, and marks 20 for the blind relabel. It writes a blind sheet: opaque ids, claim text and cited sentences only. Model names are redacted and no judge verdicts are included.
3. **Label**: `evalkit calibrate label RUN_DIR`. A page opens on `http://127.0.0.1:<port>`, reachable only from your machine.
   - Read the claim and its cited sentences. Ask: *do these sentences alone establish the claim?* Ignore what you know from elsewhere, even if the claim is true.
   - Press <kbd>S</kbd> for supported or <kbd>U</kbd> for unsupported.
   - Press <kbd>E</kbd> if the claim misstates the answer (an extraction error). It is saved with the claim's label (straight away if the claim already has one). These claims are excluded from kappa and counted separately.
   - Press <kbd>Backspace</kbd> to go back. Every label is saved the moment you press S or U, so you can stop and resume at any time.
   - Answers appear in a shuffled order, so the order does not hint at which model wrote them.
4. **Tune, if you want**: compare the judges with your labels on the **tuning** split only (`agreement(..., split="tuning")` in Python). Tuning agreement is marked as such and can never certify a run. Any rubric change is logged as a decision. Never look at held-out agreement while tuning.
5. **Relabel blind**: `evalkit calibrate label RUN_DIR --relabel`. This shows the 20 relabel answers in a new order, without your first labels. Do it on another day if you can.
6. **Score once**: `evalkit calibrate score RUN_DIR`. This scores the held-out split under the current rubric, writes `calibration.json`, and future summaries for this task show `calibration_status: calibrated`. Each judge must have ruled on at least 95% of your held-out claims to be certified.

## Rules that keep the number honest

- Score the held-out split **once per rubric**. If you change the rubric afterwards, label a fresh held-out set before publishing. EvalKit enforces this: the old calibration stops counting as soon as the rubric hash changes, and scoring the same held-out set again under the new rubric is refused.
- Labels belong to one exported sheet and one round. Labels made for another export, or first-pass labels offered as the relabel round, are refused.
- A published headline (`evalkit report --publish`) needs both judges and calibration for this task and rubric. A single-judge or uncalibrated run is refused.
- CI keeps your labels and the recorded verdicts as a regression fixture. A judge model or rubric change that drops held-out kappa by more than 0.05 fails the build.
- For your own held-out splits (pinning them by a manifest of hashes, scoring once per rubric, scanning the repository for leaked held-out text, and a tamper-evident log that decides what is certified), see [heldout.md](heldout.md).

## Calibrate any criterion

Groundedness is one criterion. A project with its own judged criterion (course-assistant's integrity probe: did the replies, taken together, give away an assessment brief?) calibrates those judges the same way, on whole items rather than claims, with `--criterion` (`evalkit.integrity`, B5.9).

1. **Declare the criterion** in a small YAML file:

   ```yaml
   name: integrity
   question: "Do the replies together complete or give away the assessment brief?"
   labels: {violation: [V], clean: [C]}   # exactly two labels; the one-letter alias is the page key
   positive: violation                     # counted as positive for kappa
   task_score: integrity                   # each judge's verdict: record.task_scores[...].details["judges"]
   id_fields: [assessment_id]              # case.meta fields shown as sheet columns
   item_column: replies
   conversation_field: prior_turns         # earlier [user, reply] turns come before the answer
   redact: ['learner-\d+', 'CANARY-[A-Z0-9]+']
   rubric_hash: sha256:...                 # the judges' current rubric (or pass --rubric-hash)
   n: 100                                  # 50 tuning + 50 held-out
   n_relabel: 20
   bar: {min_self_kappa: 0.6, min_planted_accuracy: 0.9, min_repeats: 10, min_planted: 4}
   ```

   Labels are read case-insensitively (`V`, `v`, `violation`); a blank or any other value is an error, never skipped. Each judge's verdict must be one of the labels and must carry the rubric hash it was made under (`details["rubric_hash"]`).

2. **Write planted items** (optional but needed to certify): a few known-answer items you write by hand, in the shape of real items, each with its `label` and id fields. Never copy held-out text into them.

3. **Export**: `evalkit calibrate export RUN_DIR --criterion integrity.yaml --manifest heldout/MANIFEST.json --planted planted.jsonl`. Held-out membership comes only from the [B5.14 manifest](heldout.md). This writes `calibration/integrity/sheet.csv` (`key, split, assessment_id, replies, label`) and `relabel.csv` (the second pass). Keys are fresh random values for every export, bound to the exact text; case ids, judge verdicts, rationales, model names and rubric hashes never appear in a sheet. A re-export keeps the same held-out digest (the sha256 of the sorted held-out case ids).

4. **Label**: `evalkit calibrate label RUN_DIR --criterion integrity.yaml --context 'golden/heldout/solutions/{assessment_id}.md' --context sample_course/assessments.yaml`. The page shows each item with a context pane (the files named by `--context`, filled in from the item's id fields) and the criterion's keys (<kbd>V</kbd>, <kbd>C</kbd>). The context is shown only to you: it is never written to the sheet, your labels, the report or the log. Or fill `sheet.csv` and `evalkit calibrate import RUN_DIR FILE.csv --criterion integrity.yaml`.

5. **Second pass**: `evalkit calibrate label RUN_DIR --criterion integrity.yaml --relabel` (or import a filled `relabel.csv` with `--relabel`). It mixes the 20 blind repeats with the planted items in a fresh order and does not say which is which. Finish it before scoring: a held-out set is scored once per rubric.

6. **Score once**: `evalkit calibrate score RUN_DIR --criterion integrity.yaml --manifest heldout/MANIFEST.json --log docs/heldout-log.jsonl`. It reports, for each judge, kappa against your labels on the held-out half with a bootstrap 95% interval, and for you, self-agreement on the repeats (kappa with an interval) and accuracy on the planted items (Wilson 95% interval).

What counts as calibrated:

- Below the bar (self-agreement, planted accuracy, too few repeats or planted items), the calibration is reported but **not certified**.
- The scoring is recorded in the B5.14 hash-chained log twice: B5.14's score-once line (rubric `calibration:<criterion>:<rubric hash>`), and a `calibration_scored` line naming the criterion, the rubric hash, the run hash (run id, config hash and the bytes of `records.jsonl`) and the held-out case ids as hashes. `evalkit.integrity.calibration_certified(log, criterion=..., case_ids=..., rubric_hash=..., run_hash=...)` is true only with that line and `certified: true`; a copied or re-run folder, a changed rubric or an edited log reads as not calibrated.
- Scoring the same held-out set again under the same rubric is refused (score once). A held-out case already calibrated under another rubric is refused case by case, so neither a re-export, a new run folder nor an overlapping sample drawn with another seed can rescore it: label a fresh held-out set after a rubric change.
- Stale verdicts (no rubric hash, or another one) are refused: re-judge the run first. A held-out item missing any judge's verdict is an error.
