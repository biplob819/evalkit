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

Case ids are Unicode NFC-normalised before hashing, so `café` typed precomposed and typed decomposed are one id (and two cases with those ids are a duplicate). ASCII ids are unchanged by NFC, so existing manifests keep their hashes. A case that is not a mapping, has no id, or has no canonical form (keys of mixed types such as `1:` next to `id:` in YAML, or a self-referencing YAML anchor) is refused with the file and position only: the command exits 2 and prints no case text.

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

- **Rubric hashes** are stripped and lowercased before they are compared or written, so `ABCDEF`, `abcdef` and ` abcdef ` are one rubric. The accepted form is 1 to 128 ASCII letters, digits or `._:+-`, starting with a letter or digit: a hex digest, `sha256:<hex>`, or a name such as `groundedness-v3`. Anything else is refused.
- **`case_ids`** must be a collection of string or integer ids. A bare string is refused (its characters would otherwise each become an id).
- **One writer at a time.** The check and the append run under an exclusive lock on the log file (`fcntl.flock` on Linux and macOS, `msvcrt.locking` on Windows), so two processes scoring the same case at once cannot both succeed: one waits, then gets `AlreadyScored`. The operating system releases the lock when the process ends, so a crash leaves nothing to clean up.
- **The log enforces the rule too.** `append_entry` refuses a `heldout_scored` line that re-scores a case, and `verify-log` reports one however it got into the file (see section 4).
- **Calibration lines are reserved.** `append_entry` and `record_scoring` refuse a `calibration_scored` entry and any `heldout_scored` entry whose rubric starts `calibration:`. Those are written only by EvalKit's criterion calibration (`evalkit.integrity`, see [calibrate-your-task.md](calibrate-your-task.md#calibrate-any-criterion)), as a pair of lines in one write, so a crash leaves both or neither.

A scoring counts as certified only when the log holds a matching line:

```python
from evalkit.heldout import is_certified

is_certified("heldout/log.jsonl", case_ids, rubric_hash)   # exactly these ids, this rubric, chain intact
```

`is_certified` never raises: an unreadable log, a broken chain, a re-scored case anywhere in the log, or an argument of the wrong type all give `False`.

## 3. Fail the build when held-out text leaks

Run this where the private held-out file exists (the owner's machine, or a protected CI job):

```bash
python -m evalkit.heldout scan --cases heldout/cases.jsonl
```

It prints `path<TAB>manifest_id` for each tracked file that holds held-out text, and exits 1. A case leaks when one of its strings (25 characters or more) appears in a file, or when any 10 consecutive words of one do. Both sides are compared after Unicode NFKC normalisation, removing invisible characters (zero-width spaces and joiners, word joiners, soft hyphens, BOMs, bidirectional marks, variation selectors; a file holding them is also read with each taken as a space), dropping combining accents (so `résumé` matches `resume`), casefolding, folding common Cyrillic, Greek and Armenian look-alikes to the ASCII letters they resemble, and turning every run of punctuation or whitespace into one space, so re-wrapping, quoting, re-casing, full-width letters, zero-width characters inside or between words, look-alike letters and string concatenation do not hide a leak. Each file is also compared after JSON unescaping (`'`), after HTML unescaping (`&#39;`, `&nbsp;`), with words broken over a line by a hyphen joined again (`contami-` then `nation` on the next line), and with NUL bytes removed. Files holding NUL bytes are scanned like any other (undecodable bytes are dropped), so a leak behind one binary byte, or in ASCII-range UTF-16 text, is still found.

To stop a leak before it is committed, scan what is staged rather than the work tree (for example in a pre-commit hook):

```bash
python -m evalkit.heldout scan --cases heldout/cases.jsonl --staged
```

`--staged` (`exposure_scan(..., index=True)`) reads each index entry's staged content, as `git show :path` would, so a file staged with a leak and then cleaned in the work tree is still caught.

Real cases often share text with public ones: an instruction template, a canary value, a SQL join. Limit the scan to the fields that hold the held-out text, and name the public cases whose shared word windows are not held-out-specific:

```bash
python -m evalkit.heldout scan --cases heldout/agent_tasks --field goal --public src/evalkit/agents/tasks
```

A whole held-out string is never excused by `--public`, so a held-out case copied into a public one is still caught. Not covered: git history (rewrite it and rotate the case), UTF-16 text outside the ASCII range and other non-UTF-8 encodings, encoded copies (base64, reversed text), look-alike letters outside the folded set (see Limits), and paraphrases (see the paraphrase contamination probe, B5.8).

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

`verify-log` checks the chain and the score-once rule on the log itself: a malformed `heldout_scored` line, or one that scores a case already scored under the same normalised rubric hash, is a problem (exit 1). It then compares the log with the committed file at `HEAD`, at the push base (on a new branch, the merge-base with the default branch), and at every parent of `HEAD`: the working log must start with each. Every commit between the base and `HEAD`, merges included, must keep each parent's log as a prefix, so a rewrite that was later restored is still caught, and so is a merge resolution that drops a line. At the default branch tip the two logs must agree on their common length.

The check fails closed:

- The log must be tracked at `HEAD` under its exact path. Paths are compared case-sensitively; on a case-insensitive file system (macOS, Windows) a path typed in another case is resolved to the one tracked path that is the same file, never read as an empty log.
- Only "the file is absent at that revision" counts as an empty log. Any other git failure (an unreadable object, a folder at that path) is a problem.
- A revision missing from the clone is a problem.
- In a shallow clone, `HEAD` or any commit the check walks whose parents were cut off is a problem, with a message telling CI to fetch the full history (`actions/checkout` with `fetch-depth: 0`, or `git fetch --unshallow`). With `--base` or `--default-branch` on a depth-1 clone, `verify-log` exits 2 with that message.

Two branches that both append cannot both keep a valid chain, so the merge fails: rebase the later branch and append again.

A deliberate history rewrite by someone with commit rights is outside what a repository-local check can stop (course-assistant D-55); it stays visible in git history and review.

## Limits

- **The manifest hash is not a signature.** `manifest_sha256` is an unkeyed sha256: it catches a careless hand edit, but anyone who edits a case and rebuilds the manifest gets a valid one. What stops a deliberate change is git review of `MANIFEST.json` (protect it with CODEOWNERS or required review). The log's chain is unkeyed too.
- **Anyone who can write the log file can forge a whole chain by hand.** The chain, the score-once rule and the refusals in `append_entry` and `record_scoring` stop forgery through EvalKit's own functions and keep an honest crash from leaving half a record. They cannot stop someone from editing the file directly and recomputing every `prev`. Protection against that rests on git review of every change to the log plus the CI `verify-log` check in section 4: `verify-log` refuses any change to the committed log other than appending, and review is what catches a line appended by hand rather than by EvalKit. Run both.
- **Only common look-alike letters are folded.** NFKC maps full-width and compatibility forms to plain letters; on top of it the scan folds a fixed set of Cyrillic, Greek and Armenian letters that look like ASCII ones (`а` to `a`, `ο` to `o`, ...; `evalkit.heldout.HOMOGLYPHS`), on both sides, so text in those scripts still matches itself. Other look-alikes (rarer scripts, mathematical symbols NFKC leaves alone) are missed. Case ids get NFC only for hashing; `ids_in_text` also reads text NFKC-folded, without invisible characters, casefolded and with the look-alikes folded, so a full-width, zero-width-split or upper-case spelling of a lower-case id is found.
- **The lock covers one file system.** It serialises writers that share the log file; two clones that each append and then merge are caught by `verify-log` instead (rebase and append again).
