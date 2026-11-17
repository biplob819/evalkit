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
