# B5.14 — Held-out discipline and tamper-evident logs

**Wave 5. Depends on:** A2.6, C3.1. **Unblocks:** B5.7 (ledger), B5.11 (security kit held-out handling). Decisions: D-61 to D-66.

Skeleton written by the lead so the lane can start (D-66); the lane's builder completed Goal, Why, Interface and Out of scope before tests.

## Capabilities
- **HLD-01**: Held-out split pinned by a committed manifest of hashes and counts; reports carry split-level aggregates only [D-65]
- **HLD-02**: Score-once guard: each held-out case is scored once per rubric version [D-65]
- **HLD-03**: Exposure scan: the build fails if held-out text appears in tracked files [D-65]
- **HLD-04**: Tamper-evident log: hash-chained append-only JSONL checked in CI against the push base, every merge parent and the default branch tip; a calibration is certified only with a matching logged line (case ids and rubric hash) (course-assistant, its D-55/D-59) [D-65]

## Goal
One public home, `evalkit.heldout`, for the held-out rules every project re-implements: pin a held-out split by hashes, score each held-out case once per rubric, fail the build when held-out text leaks into the repository, and keep a hash-chained, append-only log that decides what counts as certified.

## Why
course-assistant built all four by hand (its D-54 exposure guard, D-55 tuning log, D-57/D-59 certified calibration); assay-pipeline pins its held-out attacks by a manifest of hashes; EvalKit pins its agent tasks the same way (AGT-04) and refuses a held-out set re-scored under another rubric (CAL-05). Each copy differs in what it catches. D-65 gives them one home that B5.7 and B5.11 build on, generic over any case file so a project need not adopt EvalKit's schemas.

## Interface

Module `evalkit.heldout` (public, in `scripts/check_public_api.py` `PUBLIC`). Every message names manifest ids (the first 12 hex characters of a case id's sha256), counts, file paths or revisions, never held-out text.

### Case files and hashes (HLD-01)
- `load_cases(path, *, id_field="id") -> list[dict[str, Any]]`: a `.jsonl` file (one object per line), a `.json` or `.yaml`/`.yml` file (a list of objects, one object with `id_field`, or a mapping whose only list value is the cases), or a folder (every such file directly inside, sorted). A case that is not a mapping or has no non-blank `id_field` raises `HeldoutError` naming the file and position.
- `case_id_hash(case_id: str) -> str`: sha256 hex of the id. `manifest_id(case_id: str) -> str`: its first 12 characters.
- `content_hash(case: Mapping[str, Any], *, id_field="id") -> str`: sha256 of the case without its id as canonical JSON (sorted keys, compact, ASCII; non-JSON scalars such as YAML dates as `str`), so the same case under another id is recognised.

### Manifest (HLD-01)
- `build_manifest(splits: Mapping[str, str | Path], *, id_field="id") -> dict[str, Any]`: `{"schema": "evalkit.heldout.manifest/1", "id_field", "counts": {split: n}, "splits": {split: [{"id_sha256", "content_sha256"}, ...]}, "manifest_sha256"}`; entries sorted by `id_sha256`; `manifest_sha256` is the sha256 of the canonical JSON of everything else. Raises `HeldoutError` on a duplicate id within a split, a case (by id or content) in two splits, or an empty split.
- `write_manifest(path, splits, *, id_field="id") -> dict[str, Any]`: writes it (sorted keys, indent 2, trailing newline).
- `verify_manifest(path, splits, *, id_field="id") -> list[str]`: problems, empty when the files match: unreadable manifest, unknown schema, `manifest_sha256` not matching the manifest's own body (edited by hand), a different `id_field`, a missing or unexpected split, and per split a missing, changed, unexpected or duplicate case or a count that differs. An unreadable case file is a problem, not a crash.
- `SplitAggregate` (frozen dataclass: `split`, `n`, `mean`, `lo`, `hi`, `manifest_sha256`) and `split_aggregate(values: Mapping[str, float], manifest: Mapping[str, Any], *, split="heldout", seed=0, n_resamples=10_000) -> SplitAggregate`: the mean with EvalKit's seeded percentile bootstrap interval over exactly the manifest's cases of that split; a value for a case outside the split, a split case with no value, or a non-finite value raises `HeldoutError`. The type has no per-case field, so a report built from it carries split-level aggregates only.
- `ids_in_text(text: str, manifest: Mapping[str, Any], *, split="heldout") -> list[str]`: manifest ids of cases whose id appears as a token in `text` (a report check: a held-out report must return `[]`). Tokens are runs of letters, digits and `_-.:`, also with trailing `.:` removed; ids containing other characters are not detected.

### Score-once guard (HLD-02)
A scoring is a `heldout_scored` line in the HLD-04 log: `{"kind": "heldout_scored", "rubric_hash", "case_id_sha256": [sorted], "n_cases", "at", "note"?, "prev"}`.
- `scored_before(log, case_ids: Iterable[str], rubric_hash: str) -> list[str]`: manifest ids of the given cases already scored under this rubric.
- `record_scoring(log, case_ids, rubric_hash, *, at: str | None = None, note: str | None = None) -> dict[str, Any]`: appends the line (`at` defaults to today's UTC date) and returns it. Raises `AlreadyScored` (a `HeldoutError`) when any case was already scored under this rubric, naming the count and manifest ids; `HeldoutError` for no cases, a blank rubric hash, or a broken chain. A case may be scored again under a different rubric hash (EvalKit's own calibration additionally refuses that, CAL-05).

### Exposure scan (HLD-03)
- `Exposure` (frozen, ordered dataclass: `path`, `manifest_id`) and `exposure_scan(cases: Sequence[Mapping[str, Any]], root: str | Path, *, id_field="id", fields: Sequence[str] | None = None, public: Sequence[Mapping[str, Any]] = (), min_chars=25, window=10) -> list[Exposure]`, sorted.
- Held-out text is every string value in a case (nested, the top-level id excluded), or only in the top-level `fields` when given. A `window` of words that also occurs in a `public` case (a shared instruction template) is ignored; a whole held-out string never is, so a held-out case copied into a public one is still caught. (Found on EvalKit's own agent tasks: canaries, SQL joins and one instruction template are shared between public and held-out tasks; `--field goal --public src/evalkit/agents/tasks` scans clean.) Normalised: NFKC, casefolded, every run of non-alphanumeric characters one space. A case is exposed in a file when a normalised string of at least `min_chars` characters appears in the file's normalised text on word boundaries, or when any `window` consecutive words of one of its strings (at least `min_chars` characters) do.
- Files are those `git ls-files` lists under `root`, text by content (no NUL byte), each compared as written and, where it holds JSON escapes, after unescaping. Not in scope: git history, non-UTF-8 encodings (UTF-16), paraphrases (B5.8).
- `tracked_files(root) -> list[str]`: the repository-relative paths; raises `HeldoutError` when `root` is not a git work tree.

### Tamper-evident log (HLD-04)
- `GENESIS = "0" * 64`. Each line is canonical JSON (sorted keys, compact, ASCII) with `prev` = sha256 of the previous line's text (`GENESIS` for the first).
- `read_log(path) -> list[dict[str, Any]]` (empty when absent); `append_entry(path, entry: Mapping[str, Any]) -> dict[str, Any]` (refuses a broken chain, an entry that sets `prev`, or a non-mapping; writes LF line endings).
- `chain_problems(path) -> list[str]`: a line that is not a JSON object, or whose `prev` does not name the line before (edited, dropped, inserted or reordered).
- `append_only_problems(path, revisions: Sequence[str], *, default_tip: str | None = None, root: str | Path | None = None) -> list[str]`: by `git show <rev>:<path>`: the working file must start with the file at `HEAD` and at each revision (absent at a revision = empty); every commit in `<rev>..HEAD` (full history, merges included) must keep the file of each of its parents as a prefix, so a rewrite later restored, or a merge resolution that drops lines, is caught at that commit; at `default_tip` the two must agree on their common length. A revision not in the clone, or no git repository, is a problem (fail closed).
- `ci_revisions(root, *, base: str | None = None, default_branch: str | None = None) -> tuple[list[str], str | None]`: the revisions CI compares with: the push base (an all-zero or empty base on a new branch means the merge-base with the default branch tip) and every parent of `HEAD`; plus the default branch tip (`origin/<branch>`, else `<branch>`).
- `is_certified(log, case_ids: Iterable[str], rubric_hash: str) -> bool`: true only when the log exists, its chain holds, and a `heldout_scored` line has this rubric hash and exactly these case ids (by hash). Append-only against git is the CI check, not part of this call.

### Errors
`HeldoutError(ValueError)`; `AlreadyScored(HeldoutError)`.

### Command line: `python -m evalkit.heldout`
- `manifest --split NAME=PATH [--split ...] [--id-field id] --out FILE`: writes the manifest; prints split counts and the manifest hash.
- `verify --manifest FILE --split NAME=PATH [...] [--id-field id]`: prints each problem.
- `scan --cases PATH [--id-field id] [--root .] [--field F ...] [--public PATH ...] [--min-chars 25] [--window 10]`: prints `path<TAB>manifest_id` per exposure, then `exposed: N held-out case(s) in M of K tracked file(s)` (or `no held-out text in K tracked file(s)`).
- `verify-log LOG [--rev REV ...] [--base SHA] [--default-branch NAME]`: the chain plus append-only against `--rev` and, when `--base` or `--default-branch` is given, `ci_revisions`.
- Exit codes are EvalKit's: 0 clean, 1 a check failed, 2 an input error (unreadable or invalid file, not a git repository). Output never includes held-out text.

## Out of scope
- Wiring `evalkit calibrate score` or `agent-run` to the log or manifest (B5.7 and B5.11 build on this); EvalKit's own `heldout/MANIFEST.json` (AGT-04 format) is unchanged.
- A keyed or signed log: a deliberate history rewrite by someone with commit rights is outside what a repo-local check can stop (course-assistant D-55); it stays visible in git history and review.
- Scanning git history, non-UTF-8 files, binary files, or paraphrased leaks (B5.8).
- Concurrent writers to one log (one process appends at a time; a merge of two branches that both appended fails the append-only check, so the later branch rebases).
