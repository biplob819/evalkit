# B5.9 — Calibration integrity sheet

**Wave 5. Depends on:** A2.6, B5.2, B5.14 (D-66). Decisions: D-62, D-65, D-69. Release: v0.4.0.

Skeleton by the lead; the lane builder completed Goal, Why, Interface and Out of scope before tests. Requirements from course-assistant are in `inputs.md`.

## Capabilities
- **CIS-01**: Integrity sheet: blind repeats and planted items mixed into a second labelling pass through the labelling page [D-62]
- **CIS-02**: Labeller self-agreement and planted accuracy reported with CIs; calibration is not certified below the bar; the certificate is bound to the run and rubric hashes [D-62]
- **CIS-03**: Calibration sheets for any criterion (course-assistant's integrity sheet, its D-53/D-66): a declared label set and its mapping to judge outputs (e.g. V/C on a whole conversation from task_scores details), context shown only to the labeller (never in sheets or reports), the labelling page with a context pane and the label keys, opaque keys bound to the exact item, per-judge kappa with a bootstrap CI on the held-out half, refusal of stale or re-rubricked verdicts, a blind relabel round; course-assistant's cases are the tests [D-69]

## Goal
One public module, `evalkit.integrity`, that calibrates the judges of any declared two-way criterion against the owner's hand labels, the way A2.6 calibrates groundedness: a blind sheet of whole items (a conversation, an answer) with opaque keys, the A2.6 labelling page with a context pane only the labeller sees, per-judge kappa with a bootstrap CI on the held-out half, and a second labelling pass that mixes blind repeats with planted known-answer items, so the labeller's own consistency is measured before the judges are trusted. A calibration counts only through a `calibration_scored` line in B5.14's hash-chained log, bound to the run, the rubric and the held-out case ids.

## Why
course-assistant hand-built a second calibration (its integrity probe: V/C on whole conversations, E2.3) beside EvalKit's groundedness one, copying `_opaque`, `_allocate` and `_Pair` from EvalKit internals; each verifier pass found a new way to rescore a held-out set under another rubric (D-53, D-66). D-62 asked for labeller-consistency checks (blind repeats, planted items) and D-69 widened B5.9 to calibration sheets for any criterion. Built once on B5.14's manifest, score-once guard and log, every project gets the same rules, and course-assistant drops its private copy at v0.4.0.

## Interface

Module `evalkit.integrity` (public, in `scripts/check_public_api.py` `PUBLIC`). Messages name counts, keys, paths and manifest ids (B5.14), never case ids, item text or context text.

### Criterion (CIS-03)
- `Criterion` (frozen dataclass): `name` (lowercase `[a-z0-9][a-z0-9._-]*`), `labels: Mapping[str, tuple[str, ...]]` (exactly two canonical labels, lowercase, each with aliases; the first one-character alias of each is its key on the labelling page), `positive` (the label counted as positive for kappa), `question` (shown on the page), `task_score` (the `Record.task_scores` key; default `name`), `judges_field` (`"judges"`: judge name -> verdict in that task score's details), `rubric_field` (`"rubric_hash"`), `id_fields: tuple[str, ...]` (`Case.meta` fields shown as sheet columns, e.g. `assessment_id`), `item_column` (`"item"`; course-assistant: `"replies"`), `conversation_field: str | None` (a `Case.meta` list of `[user, reply]` turns whose replies come before the answer), `redact: tuple[str, ...]` (regular expressions replaced by `[redacted]` in item text, after EvalKit's model-name `redact`), `judges: tuple[str, ...]` (expected judges; empty means every judge seen on the sample), `rubric_hash: str | None`, `n=100`, `n_relabel=20`, `bar: ConsistencyBar`.
- `Criterion.parse(raw: str) -> str`: canonical label for a label or alias, case-insensitive, stripped. Blank or unknown raises `SheetError` (never skipped).
- `load_criterion(path) -> Criterion`: from YAML with the same field names (`labels` a mapping of label to alias list; `bar` a mapping). Unknown fields, a label set that is not two labels, a missing or duplicate page key, or a `positive` not in the set raise `SheetError`.
- `INTEGRITY` : the course-assistant criterion (`integrity`, `violation: [V]`, `clean: [C]`, positive `violation`, `id_fields=("assessment_id",)`, `item_column="replies"`, `conversation_field="prior_turns"`).

### Items and sample (CIS-03)
- `Item` (frozen dataclass): `case_id` (private join key), `parts: tuple[str, ...]` (redacted text the labeller sees: the replies of a conversation), `ids: Mapping[str, str]`, `verdicts: Mapping[str, str]` (judge -> canonical label), `rubric_hash: str | None`.
- `items_from_run(records, cases, criterion, *, model=None) -> list[Item]`: one item per case whose record has the criterion's task score (not `applicable: false`) and an answer; verdicts read from `task_scores[criterion.task_score].details[criterion.judges_field]`, each parsed by `Criterion.parse`. Two records for one case (several models) without `model=` raises `SheetError`.
- `PlantedItem` (frozen dataclass: `id`, `parts`, `ids`, `label`) and `load_planted(path, criterion) -> list[PlantedItem]`: known-answer items from JSONL/JSON/YAML (`id`, `text` or `parts`, `label`, and the criterion's id fields). Never held-out text.
- `sample_items(items, *, manifest, seed, n=100, n_relabel=20) -> list[SampledItem]` (`SampledItem`: `case_id`, `split`, `relabel`): half tuning, half held-out, each half stratified by the first id field (EvalKit's largest-remainder allocation), `n_relabel` taken evenly from both halves. Held-out membership comes only from a B5.14 manifest (`manifest["splits"]["heldout"]`, its `manifest_sha256` checked): a case is held out iff its id hash is listed. Fixed seed; too few items in a half raises `SheetError`.

### Sheet and labels (CIS-01, CIS-03)
- `CriterionRow` (model): `sheet_id`, `key` (fresh random opaque value per export: a keyed digest, under a new random secret, of the case id and the exact parts), `split` (`tuning` | `heldout` | `planted`), `relabel`, `case_id` (private), `ids`, `parts`, `planted_label` (private; planted rows only).
- `build_sheet(sample, items, criterion, *, seed, planted=(), secret=None) -> list[CriterionRow]`: first-pass rows in a seeded shuffle, then the planted rows.
- `second_pass(rows, *, seed) -> list[CriterionRow]`: the relabel repeats and the planted items together, in a fresh seeded order (CIS-01). The labeller is never told which rows are which.
- `sheet_csv(rows, criterion, *, second=False, seed=0) -> str`: columns `key, split, <id fields>, <item_column>, label`; the item cell is the parts as `[1] ...` blocks; labels blank. The second-pass CSV leaves `split` blank on every row, so planted rows and repeats look alike. Never a case id, judge verdict, rubric, model name or context text.
- `CriterionLabels` (model: `sheet_id`, `round` `first` | `relabel`, `labels: {key: canonical label}`) and `labels_from_csv(text, rows, criterion, *, round_="first") -> CriterionLabels`: unknown or repeated keys, edited item text, keys outside the round, and blank or unknown labels raise `SheetError`.
- `heldout_digest(rows) -> str`: sha256 of the sorted held-out case ids joined by newlines: stable across exports (keys are fresh each time).

### Labelling page (CIS-01, CIS-03)
- `serve_sheet_labeller(rows, criterion, labels_path, *, round_="first", seed=0, context=(), port=0)`: A2.6's page (same server, same localhost-only, Host-checked, JSON-only POST), in criterion mode: the question, the id fields, the item's parts, a context pane and the criterion's label keys; Backspace goes back; every label autosaves atomically. `round_="relabel"` shows `second_pass(rows)` without first-pass labels. `context` are path templates formatted with the row's id fields (e.g. `golden/heldout/solutions/{assessment_id}.md`); files are read when an item is shown and sent only to the page, never written to the sheet, labels, records, reports or log. A template naming an unknown field or a missing file raises `SheetError` at start (naming the path, not its text). The groundedness page's API and the A2.6 sheet are unchanged.

### Scoring (CIS-02, CIS-03)
- `ConsistencyBar` (frozen dataclass): `min_self_kappa=0.6`, `min_planted_accuracy=0.9`, `min_repeats=10`, `min_planted=4`, `bound="point"` (or `"lo"`: the CI lower bound must clear the bar). `None` for a minimum drops that check.
- `Consistency` (model): `n_repeats`, `self_agreement` (raw), `self_kappa: Interval | None` (cluster bootstrap, one cluster per item), `n_planted`, `planted_correct`, `planted_accuracy: Interval | None` (Wilson 95%), `passes`, `reasons`.
- `consistency(rows, first, second, *, bar, seed, positive, n_resamples=10_000) -> Consistency`.
- `JudgeKappa` (model: `judge`, `n`, `kappa: Interval`, `agreement`, `confusion` (positive first)) and `IntegrityReport` (model: `criterion`, `rubric_hash`, `run_hash`, `scored_at`, `n_heldout`, `heldout_digest`, `agreements`, `consistency`, `certified`): aggregates only, no per-case field.
- `score_sheet(rows, first, second, items, criterion, *, rubric_hash, run_hash, manifest, log, seed, scored_at, bar=None, n_resamples=10_000) -> IntegrityReport`, in this order: labels belong to this sheet and round and are complete (the second pass too, when the sheet has one: score once, so finish it first); held-out rows agree with the manifest; every item's parts still match its key; a verdict without a rubric hash, or under another one, is refused (`SheetError`: re-judge first); a held-out row missing an expected judge's verdict is an error; then the log: a broken chain is refused, a held-out case already calibrated for this criterion under another rubric raises `RubricChanged` (per case, so a re-exported or overlapping sample is caught), and B5.14's `record_scoring` under the rubric `calibration:<criterion>:<rubric_hash>` refuses (`AlreadyScored`) a second scoring under the same rubric; finally a `calibration_scored` line (`CalibrationLine`) is appended: `criterion`, `rubric_hash`, `run_hash`, `case_id_sha256`, `heldout_digest`, `certified`, judge kappas, consistency figures (no case ids, no text). `certified` = the bar passes.
- `calibration_certified(log, *, criterion, case_ids, rubric_hash, run_hash) -> bool`: B5.14 `is_certified` for the namespaced rubric and these held-out case ids, and a `calibration_scored` line with this criterion, rubric, run hash and case ids whose `certified` is true. Never raises (false on any error).
- `run_hash(run_dir) -> str`: sha256 of the run id, config hash and the bytes of `records.jsonl`.
- Errors: `SheetError(ValueError)`, `RubricChanged(SheetError)`; `evalkit.heldout.AlreadyScored` passes through.

### Command line
`evalkit calibrate` keeps its groundedness behaviour; `--criterion FILE` switches each subcommand to the criterion sheet in `RUN_DIR/calibration/<name>/`:
- `export RUN_DIR --criterion C --manifest M [--planted P] [--force]`: `sheet.json` (private), `sheet.csv`, `relabel.csv` (second pass).
- `label RUN_DIR --criterion C [--relabel] [--context TEMPLATE ...] [--port N]`.
- `import RUN_DIR CSV --criterion C [--relabel]`.
- `score RUN_DIR --criterion C --manifest M --log LOG [--rubric-hash H]`: writes `calibration.json`; prints kappa per judge, the consistency figures and certified or not.
Exit codes are EvalKit's (2 for a refused input).

## Out of scope
- Multi-class label sets (kappa here is binary, through `evalkit.calibration`).
- Labeller consistency (repeats and planted items) for A2.6 groundedness: its sheet and page API stay byte-identical; a project can declare groundedness-like criteria here.
- Hiding the private `sheet.json` from a labeller with access to the run folder: blinding covers the page and the CSVs.
- Re-scoring a held-out set under the same rubric after a later relabel (B5.14 scores once per rubric; course-assistant's E2.3 allowed it): finish the second pass before scoring.
- Wiring the result into `summary.json` or `--publish` (B5.7 reads `calibration_certified`).
