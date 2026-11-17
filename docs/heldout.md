# Keep your held-out split honest

A held-out split measures generalisation only while nobody has tuned on it, re-scored it until it looked good, or pasted it into the repository. `evalkit.heldout` (new in 0.4) gives every project the same four guards, over any case file with an id field (`.jsonl`, `.json`, `.yaml`, or a folder of them). Nothing it prints or writes holds held-out text: only manifest ids (the first 12 hex characters of a case id's sha256), counts, file paths and revisions.

| Guard | What it stops | Python | Command line |
|---|---|---|---|
| Manifest (HLD-01) | A held-out case edited, added, dropped or moved between splits | `build_manifest`, `write_manifest`, `verify_manifest` | `manifest`, `verify` |
| Aggregates only (HLD-01) | Per-case held-out rows in a report | `split_aggregate`, `ids_in_text` | |
| Score once (HLD-02) | Scoring a held-out case twice under one rubric | `record_scoring`, `scored_before` | |
| Exposure scan (HLD-03) | Held-out text in a tracked file | `exposure_scan` | `scan` |
| Tamper-evident log (HLD-04) | An edited, dropped or reordered log line; a rewritten history | `append_entry`, `chain_problems`, `append_only_problems`, `is_certified` | `verify-log` |

The command line is `python -m evalkit.heldout <command>`. Exit codes are EvalKit's: 0 clean, 1 a check failed, 2 an input error.

## 1. Pin the split with a manifest

Keep the held-out cases out of git (for example `heldout/*` in `.gitignore`) and commit only the manifest:

```bash
python -m evalkit.heldout manifest --split heldout=heldout/cases.jsonl --split public=data/cases.jsonl --out heldout/MANIFEST.json
python -m evalkit.heldout verify --manifest heldout/MANIFEST.json --split heldout=heldout/cases.jsonl --split public=data/cases.jsonl
```

The manifest holds, per split, the sha256 of each case id and of each case's content (without its id), the case counts, and a `manifest_sha256` over all of it. `verify` fails on any drift: a changed, missing, unexpected or duplicate case, a different count, a missing split, or a manifest edited by hand. Building refuses a case that sits in two splits, by id or by content.

Report held-out results only as split-level aggregates:

```python
from evalkit.heldout import split_aggregate, ids_in_text
import json

manifest = json.load(open("heldout/MANIFEST.json"))
agg = split_aggregate(per_case_scores, manifest)   # n, mean, 95% bootstrap interval
assert ids_in_text(report_html, manifest) == []    # no held-out id in the report
```

`split_aggregate` refuses a score for a case outside the split, a held-out case with no score, and a non-finite score, so the number always covers exactly the pinned split.

## 2. Score each held-out case once per rubric

```python
from evalkit.heldout import record_scoring, AlreadyScored

record_scoring("heldout/log.jsonl", case_ids, rubric_hash, note="groundedness v3")
```

Call it before anyone sees the scores. A second scoring of any of these cases under the same rubric hash raises `AlreadyScored`, naming how many cases and their manifest ids. The line is written to the tamper-evident log below; it holds the case id hashes, never the ids or the text. A new rubric hash may score the cases again; EvalKit's own judge calibration is stricter and refuses that too (see [calibrate-your-task.md](calibrate-your-task.md)).

A scoring counts as certified only when the log holds a matching line:

```python
from evalkit.heldout import is_certified

is_certified("heldout/log.jsonl", case_ids, rubric_hash)   # exactly these ids, this rubric, chain intact
```

## 3. Fail the build when held-out text leaks

Run this where the private held-out file exists (the owner's machine, or a protected CI job):

```bash
python -m evalkit.heldout scan --cases heldout/cases.jsonl
```

It prints `path<TAB>manifest_id` for each tracked file that holds held-out text, and exits 1. A case leaks when one of its strings (25 characters or more) appears in a file, or when any 10 consecutive words of one do. Both sides are compared after Unicode normalisation, casefolding and turning every run of punctuation or whitespace into one space, so re-wrapping, quoting, re-casing, string concatenation and JSON escaping do not hide a leak. Binary files are skipped.

Real cases often share text with public ones: an instruction template, a canary value, a SQL join. Limit the scan to the fields that hold the held-out text, and name the public cases whose shared word windows are not held-out-specific:

```bash
python -m evalkit.heldout scan --cases heldout/agent_tasks --field goal --public src/evalkit/agents/tasks
```

A whole held-out string is never excused by `--public`, so a held-out case copied into a public one is still caught. Not covered: git history (rewrite it and rotate the case), UTF-16 files, and paraphrases (see the paraphrase contamination probe, B5.8).

## 4. Keep the log append-only

Each log line is canonical JSON whose `prev` field is the sha256 of the line before it (64 zeros for the first). Editing, dropping, inserting or reordering a line breaks the chain, and `append_entry` refuses to write to a broken log. In CI, also check that the committed history only ever appended:

```yaml
# .github/workflows/ci.yml (needs the full history)
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- run: >
    uv run python -m evalkit.heldout verify-log heldout/log.jsonl
    --base "${{ github.event.before || github.event.pull_request.base.sha }}"
    --default-branch main
```

`verify-log` checks the chain, then compares the log with `git show <rev>:<path>` at `HEAD`, at the push base (on a new branch, the merge-base with the default branch), and at every parent of `HEAD`: the working log must start with each. Every commit between the base and `HEAD`, merges included, must keep each parent's log as a prefix, so a rewrite that was later restored is still caught, and so is a merge resolution that drops a line. At the default branch tip the two logs must agree on their common length. A revision missing from the clone fails the check.

Two branches that both append cannot both keep a valid chain, so the merge fails: rebase the later branch and append again.

A deliberate history rewrite by someone with commit rights is outside what a repository-local check can stop (course-assistant D-55); it stays visible in git history and review.
