# B5.9 inputs: course-assistant (ai-teaching-assistant-0e, 2026-11-17)

Build both (a) calibration sheets for any criterion and (b) labeller consistency.

- Label set: a two-way label on a whole conversation, `V` (violation: replies together complete or give away an assessment brief) or `C` (clean); parser also accepts `violation`/`clean`, case-insensitive; blank or other value is an error, never skipped.
- Mapping: each judge returns "violation" or "clean" for the conversation, in `record.task_scores["integrity"].details["judges"]` (judge name -> verdict). Kappa per judge, labeller V/C vs that judge, on held-out rows only, with a bootstrap CI. A held-out row missing a judge's verdict is an error.
- Labeller-only context: the brief's title and sessions (`sample_course/assessments.yaml`) and the private solution file `golden/heldout/solutions/<assessment_id>.md` (git-ignored). The sheet never contains the solution, brief text, any judge verdict or rationale, or a model name.
- Sheet columns: `key, split, assessment_id, replies, label`; `key` a fresh random opaque value per export, bound to the exact replies; `replies` redacted (learner ids and canaries stripped); `case_id` private (join key), never in the CSV.
- Rules: held-out digest = sha256 of sorted held-out case ids (stable across exports); every verdict carries the current rubric hash; stale verdicts refused; no rescoring a held-out case under another rubric (per-case guard through the tuning log — use B5.14's `evalkit.heldout` score-once/log); blind relabel of 20; certified only by a logged `calibration_scored` line (B5.14 `is_certified`).
- Sample: 100 conversations, 50 public and 50 held-out; `probe_sample` uses a fixed seed and requires held-out membership from the MANIFEST (B5.14 manifest).
- Code to lift (MIT, read-only): /Users/biplob.chakraborty/AI teaching Assistant/src/course_assistant/eval/calibrate.py (docstring, probe_sample, CSV writer, _LABELS, _judge_verdicts, _verdict_rubrics, heldout_digest, score_probe, certified_calibration); scripts/calibrate.py (export, import-integrity, score); tests/eval/test_calibration.py (from verifier passes 1-5 of E2.3).
- Preferred: integrity uses EvalKit's A2.6 labelling page (with a context pane and V/C keys) rather than a CSV.
- Adoption: course-assistant moves at v0.4.0 after the owner approves the publish.
