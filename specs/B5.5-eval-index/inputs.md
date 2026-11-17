# B5.5 inputs (collected before the spec)

## Portfolio site (portfolio-site session, 2026-11-17)
The site's `portfolio sync` reads, per project, from `portfolio.yml` `story`. The index should emit these keys or a one-to-one mapping:
- `headline`: {value, label, note}, with the CI and run id in the note
- `evals`: {name, data, method, result, status: pass|final|provisional|fail|pending, url}
- `improvements`: {metric, before, after, change, url}, from the B5.7 experiment ledger (before and after with CIs from compare)
- `learnings`: {lesson, now, url}, linking each project's lessons.md
- `monitoring`: {metric, kind: north-star|adoption|quality|guardrail|cost, target, current, how}, from B5.3 gates and the B5.7 alarm bands

## Projects
Seven downstream projects (docs/downstream.tsv). EvalKit is the eighth and appears on the index too. A summary is either EvalKit's `RunSummary` (course-assistant, gemma-grounded-health) or the B5.3 metrics-summary format (assay-pipeline, respectful-offers, others as they adopt it). The `simulated` flag must be shown (D-63).
