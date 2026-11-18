"""B5.14: held-out discipline and tamper-evident logs (HLD-01..04, D-65).

Every held-out case here is synthetic (tests/fixtures/heldout/). The git checks run against
temporary repositories built per test; nothing here reads EvalKit's private held-out files.
"""

from __future__ import annotations

import hashlib
import json
import random
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import pytest
import yaml

from evalkit import heldout as H

FIXTURES = Path(__file__).parent / "fixtures" / "heldout"
CASES = FIXTURES / "cases.jsonl"
PUBLIC = FIXTURES / "public.yaml"
GOLDEN_MANIFEST = FIXTURES / "MANIFEST.json"
GOLDEN_LOG = FIXTURES / "log.jsonl"
AT = "2026-11-17"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cases() -> list[dict]:
    return [json.loads(x) for x in CASES.read_text(encoding="utf-8").splitlines()]


def _texts() -> list[str]:
    """Every string a held-out fixture case carries (what must never be printed)."""
    out = []
    for c in _cases():
        out += [c["input"], c["reference"]]
    return out


def _git(root: Path, *args: str, check: bool = True) -> str:
    done = subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@t",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "init.defaultBranch=main",
            "-c",
            "core.autocrlf=false",
            *args,
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=check,
    )
    return done.stdout.strip()


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q")
    return root


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _entry(n: int) -> dict:
    return {"kind": "note", "n": n}


# =============================== HLD-01 manifest ===============================


def test_load_cases_reads_jsonl_json_yaml_and_folders(tmp_path):
    rows = [{"id": "a", "q": "x"}, {"id": "b", "q": "y"}]
    (tmp_path / "c.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n\n", encoding="utf-8"
    )
    (tmp_path / "c.json").write_text(json.dumps(rows), encoding="utf-8")
    (tmp_path / "wrapped.yaml").write_text(
        yaml.safe_dump({"version": 1, "tasks": rows}), encoding="utf-8"
    )
    one = tmp_path / "dir"
    one.mkdir()
    (one / "b.yaml").write_text(yaml.safe_dump(rows[1]), encoding="utf-8")
    (one / "a.yml").write_text(yaml.safe_dump(rows[0]), encoding="utf-8")
    (one / "notes.txt").write_text("ignored", encoding="utf-8")
    for name in ("c.jsonl", "c.json", "wrapped.yaml", "dir"):
        assert H.load_cases(tmp_path / name) == rows, name
    assert [c["id"] for c in H.load_cases(PUBLIC)] == ["fx-p1", "fx-p2"]


def test_load_cases_errors_name_file_and_position_not_text(tmp_path):
    secret = "a held-out question that must never be echoed back"
    bad = tmp_path / "bad.jsonl"
    bad.write_text(
        json.dumps({"id": "a", "q": secret}) + "\n" + json.dumps({"q": secret}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(H.HeldoutError) as err:
        H.load_cases(bad)
    assert "bad.jsonl" in str(err.value) and "2" in str(err.value)
    assert secret not in str(err.value)
    broken = tmp_path / "broken.jsonl"
    broken.write_text('{"id": "a", "q": "' + secret + '"\n', encoding="utf-8")
    with pytest.raises(H.HeldoutError) as err:
        H.load_cases(broken)
    assert secret not in str(err.value)
    with pytest.raises(H.HeldoutError):
        H.load_cases(tmp_path / "missing.jsonl")


def test_manifest_shape_holds_hashes_and_counts_only():
    """contract: the manifest format, and no id or text in it."""
    m = H.build_manifest({"heldout": CASES, "public": PUBLIC})
    assert set(m) == {"schema", "id_field", "counts", "splits", "manifest_sha256"}
    assert m["schema"] == "evalkit.heldout.manifest/1" and m["id_field"] == "id"
    assert m["counts"] == {"heldout": 3, "public": 2}
    first = _cases()[0]
    assert {"id_sha256": _sha("fx-h1"), "content_sha256": H.content_hash(first)} in m["splits"][
        "heldout"
    ]
    assert H.case_id_hash("fx-h1") == _sha("fx-h1") and H.manifest_id("fx-h1") == _sha("fx-h1")[:12]
    without_id = {k: v for k, v in first.items() if k != "id"}
    canonical = json.dumps(without_id, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    assert H.content_hash(first) == _sha(canonical)  # the id is hashed on its own
    body = {k: v for k, v in m.items() if k != "manifest_sha256"}
    assert m["manifest_sha256"] == _sha(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    )
    text = json.dumps(m)
    assert "fx-h1" not in text and not any(t in text for t in _texts())


def test_manifest_of_the_fixture_matches_the_committed_golden(tmp_path):
    """golden: the committed manifest pins the fixture split exactly."""
    golden = json.loads(GOLDEN_MANIFEST.read_text(encoding="utf-8"))
    assert H.build_manifest({"heldout": CASES, "public": PUBLIC}) == golden
    assert H.verify_manifest(GOLDEN_MANIFEST, {"heldout": CASES, "public": PUBLIC}) == []
    out = tmp_path / "M.json"
    H.write_manifest(out, {"heldout": CASES, "public": PUBLIC})
    assert out.read_bytes() == GOLDEN_MANIFEST.read_bytes()


def test_manifest_is_identical_whatever_the_file_order_or_key_order(tmp_path):
    """repro: line order, key order and folder layout do not change the manifest."""
    rows = _cases()
    shuffled = tmp_path / "shuffled.jsonl"
    order = rows[:]
    random.Random(7).shuffle(order)
    shuffled.write_text(
        "".join(json.dumps(dict(reversed(list(r.items())))) + "\n" for r in order),
        encoding="utf-8",
    )
    folder = tmp_path / "folder"
    folder.mkdir()
    for r in rows:
        (folder / f"{r['id']}.yaml").write_text(yaml.safe_dump(r), encoding="utf-8")
    a = H.build_manifest({"heldout": CASES})
    assert H.build_manifest({"heldout": shuffled}) == a
    assert H.build_manifest({"heldout": folder}) == a
    assert H.build_manifest({"heldout": CASES}) == a


def test_manifest_verify_catches_changed_missing_unexpected_and_duplicate_cases(tmp_path):
    """logical: any drift from the committed manifest is a problem, named by manifest id."""
    manifest = tmp_path / "M.json"
    held = tmp_path / "held.jsonl"
    rows = _cases()
    held.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    H.write_manifest(manifest, {"heldout": held})
    assert H.verify_manifest(manifest, {"heldout": held}) == []

    def verify_with(new_rows: list[dict]) -> list[str]:
        held.write_text("".join(json.dumps(r) + "\n" for r in new_rows), encoding="utf-8")
        return H.verify_manifest(manifest, {"heldout": held})

    edited = [dict(rows[0], reference="changed"), *rows[1:]]
    assert verify_with(edited) == [f"case {H.manifest_id('fx-h1')} changed (heldout)"]
    dropped = verify_with(rows[1:])
    assert f"missing case {H.manifest_id('fx-h1')} (heldout)" in dropped
    assert "split heldout holds 2 case(s), the manifest says 3" in dropped
    extra = verify_with([*rows, {"id": "fx-new", "input": "x"}])
    assert f"unexpected case {H.manifest_id('fx-new')} (heldout)" in extra
    dup = verify_with([*rows, rows[0]])
    assert f"duplicate case {H.manifest_id('fx-h1')} (heldout)" in dup
    renamed = verify_with([dict(rows[0], id="fx-h1b"), *rows[1:]])
    assert any("missing" in p for p in renamed) and any("unexpected" in p for p in renamed)
    held.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    assert H.verify_manifest(manifest, {"heldout": held, "public": PUBLIC}) == [
        "unexpected split public"
    ]
    assert H.verify_manifest(manifest, {}) == ["missing split heldout"]
    assert H.verify_manifest(manifest, {"heldout": held}, id_field="key") != []
    assert H.verify_manifest(manifest, {"heldout": tmp_path / "gone.jsonl"}) != []


def test_manifest_hand_edit_is_caught_by_its_own_hash(tmp_path):
    manifest = tmp_path / "M.json"
    H.write_manifest(manifest, {"heldout": CASES})
    data = json.loads(manifest.read_text(encoding="utf-8"))
    data["counts"]["heldout"] = 4
    manifest.write_text(json.dumps(data), encoding="utf-8")
    problems = H.verify_manifest(manifest, {"heldout": CASES})
    assert "manifest_sha256 does not match the manifest (edited by hand)" in problems
    manifest.write_text("{not json", encoding="utf-8")
    assert H.verify_manifest(manifest, {"heldout": CASES}) == [
        f"manifest {manifest.name} is unreadable or not JSON"
    ]
    manifest.write_text(json.dumps({**data, "schema": "other/9"}), encoding="utf-8")
    assert any("schema" in p for p in H.verify_manifest(manifest, {"heldout": CASES}))


def test_manifest_refuses_a_case_in_two_splits_and_empty_splits(tmp_path):
    leak = tmp_path / "public.jsonl"
    leak.write_text(json.dumps(_cases()[1]) + "\n", encoding="utf-8")
    with pytest.raises(H.HeldoutError, match=H.manifest_id("fx-h2")):
        H.build_manifest({"heldout": CASES, "public": leak})
    same_text = tmp_path / "same.jsonl"  # the same case under another id
    same_text.write_text(json.dumps(dict(_cases()[1], id="fx-copy")) + "\n", encoding="utf-8")
    with pytest.raises(H.HeldoutError, match=H.manifest_id("fx-copy")):
        H.build_manifest({"heldout": CASES, "public": same_text})
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(H.HeldoutError, match="no cases"):
        H.build_manifest({"heldout": empty})
    dup = tmp_path / "dup.jsonl"
    dup.write_text(
        json.dumps(_cases()[0]) + "\n" + json.dumps(_cases()[0]) + "\n", encoding="utf-8"
    )
    with pytest.raises(H.HeldoutError, match="duplicate"):
        H.build_manifest({"heldout": dup})


@pytest.mark.safety
def test_split_aggregate_carries_no_per_case_rows_and_refuses_strays():
    """safety: held-out results leave only as split-level aggregates over exactly the split."""
    m = H.build_manifest({"heldout": CASES, "public": PUBLIC})
    agg = H.split_aggregate({"fx-h1": 1.0, "fx-h2": 0.0, "fx-h3": 1.0}, m, n_resamples=200)
    assert isinstance(agg, H.SplitAggregate)
    assert (agg.split, agg.n) == ("heldout", 3)
    assert agg.mean == pytest.approx(2 / 3) and agg.lo <= agg.mean <= agg.hi
    assert agg.manifest_sha256 == m["manifest_sha256"]
    assert set(vars(agg)) == {"split", "n", "mean", "lo", "hi", "manifest_sha256"}
    with pytest.raises(H.HeldoutError, match="outside"):
        H.split_aggregate({"fx-h1": 1, "fx-h2": 0, "fx-h3": 1, "fx-p1": 1}, m)
    with pytest.raises(H.HeldoutError, match="no value"):
        H.split_aggregate({"fx-h1": 1.0, "fx-h2": 0.0}, m)
    with pytest.raises(H.HeldoutError, match="finite"):
        H.split_aggregate({"fx-h1": 1.0, "fx-h2": float("nan"), "fx-h3": 1.0}, m)
    with pytest.raises(H.HeldoutError, match="split"):
        H.split_aggregate({"fx-h1": 1.0}, m, split="nope")
    again = H.split_aggregate({"fx-h3": 1.0, "fx-h2": 0.0, "fx-h1": 1.0}, m, n_resamples=200)
    assert again == agg


@pytest.mark.safety
def test_ids_in_text_finds_heldout_ids_in_a_report():
    m = H.build_manifest({"heldout": CASES, "public": PUBLIC})
    clean = "held-out: n=3, mean 0.67 [0.33, 1.00]; public fx-p1 passed."
    assert H.ids_in_text(clean, m) == []
    leaky = "<td>fx-h2</td> and case fx-h3: failed. See (fx-h1)."
    assert H.ids_in_text(leaky, m) == sorted(H.manifest_id(i) for i in ("fx-h1", "fx-h2", "fx-h3"))
    assert H.ids_in_text("fx-p1", m, split="public") == [H.manifest_id("fx-p1")]


@pytest.mark.safety
def test_verify_problems_never_contain_case_text_or_ids(tmp_path):
    manifest = tmp_path / "M.json"
    held = tmp_path / "held.jsonl"
    rows = _cases()
    held.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    H.write_manifest(manifest, {"heldout": held})
    tampered = [dict(r, input=r["input"] + " (edited)") for r in rows[:2]]
    tampered.append({"id": "fx-new", "input": "a brand new case text that is private"})
    held.write_text("".join(json.dumps(r) + "\n" for r in tampered), encoding="utf-8")
    problems = "\n".join(H.verify_manifest(manifest, {"heldout": held}))
    assert problems
    for secret in [*_texts(), "fx-h1", "fx-new", "a brand new case text"]:
        assert secret not in problems


# =============================== HLD-02 score once ===============================


def test_second_scoring_of_a_case_under_the_same_rubric_is_refused(tmp_path):
    """logical: once per (case, rubric); a subset or overlap is refused too."""
    log = tmp_path / "log.jsonl"
    H.record_scoring(log, ["fx-h1", "fx-h2"], "rubric-a", at=AT)
    with pytest.raises(H.AlreadyScored) as err:
        H.record_scoring(log, ["fx-h1", "fx-h2"], "rubric-a", at=AT)
    assert "2 held-out case(s)" in str(err.value) and "rubric-a" in str(err.value)
    with pytest.raises(H.AlreadyScored):
        H.record_scoring(log, ["fx-h2", "fx-h3"], "rubric-a", at=AT)
    assert H.scored_before(log, ["fx-h2", "fx-h3"], "rubric-a") == [H.manifest_id("fx-h2")]
    assert H.scored_before(log, ["fx-h3"], "rubric-a") == []
    H.record_scoring(log, ["fx-h3"], "rubric-a", at=AT)
    H.record_scoring(log, ["fx-h1", "fx-h2"], "rubric-b", at=AT)  # a new rubric version
    assert len(H.read_log(log)) == 3
    assert issubclass(H.AlreadyScored, H.HeldoutError)
    with pytest.raises(H.HeldoutError, match="no cases"):
        H.record_scoring(log, [], "rubric-c")
    with pytest.raises(H.HeldoutError, match="rubric"):
        H.record_scoring(log, ["fx-h9"], "  ")
    assert len(H.read_log(log)) == 3


def test_score_once_refuses_on_a_broken_chain(tmp_path):
    log = tmp_path / "log.jsonl"
    H.record_scoring(log, ["fx-h1"], "rubric-a", at=AT)
    H.record_scoring(log, ["fx-h2"], "rubric-a", at=AT)
    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text(
        lines[1] + "\n", encoding="utf-8"
    )  # the first scoring dropped to score fx-h1 again
    with pytest.raises(H.HeldoutError, match="chain"):
        H.record_scoring(log, ["fx-h1"], "rubric-a", at=AT)


def test_scoring_line_shape(tmp_path):
    """contract: what a scoring line holds (hashes, never raw ids)."""
    log = tmp_path / "log.jsonl"
    row = H.record_scoring(log, ["fx-h2", "fx-h1", "fx-h1"], "rubric-a", at=AT, note="cal v1")
    assert row == {
        "kind": "heldout_scored",
        "rubric_hash": "rubric-a",
        "case_id_sha256": sorted([_sha("fx-h1"), _sha("fx-h2")]),
        "n_cases": 2,
        "at": AT,
        "note": "cal v1",
        "prev": H.GENESIS,
    }
    assert H.read_log(log) == [row]
    assert "fx-h1" not in log.read_text(encoding="utf-8")
    default = H.record_scoring(tmp_path / "other.jsonl", ["fx-h3"], "rubric-a")
    assert len(default["at"]) == 10 and "note" not in default


@pytest.mark.golden
def test_scoring_lines_reproduce_the_committed_log_bytes(tmp_path):
    """golden: the fixture log is exactly these two scorings."""
    log = tmp_path / "log.jsonl"
    H.record_scoring(log, ["fx-h1", "fx-h2"], "rubric-a", at=AT)
    H.record_scoring(log, ["fx-h3"], "rubric-a", at=AT, note="second batch")
    assert log.read_bytes() == GOLDEN_LOG.read_bytes()


def test_scoring_is_deterministic_given_at(tmp_path):
    """repro: the same scorings on two fresh logs give byte-identical files."""
    for name in ("a.jsonl", "b.jsonl"):
        H.record_scoring(tmp_path / name, ["fx-h2", "fx-h1"], "rubric-a", at=AT)
        H.record_scoring(tmp_path / name, ["fx-h3"], "rubric-b", at=AT)
    assert (tmp_path / "a.jsonl").read_bytes() == (tmp_path / "b.jsonl").read_bytes()
    assert b"\r\n" not in (tmp_path / "a.jsonl").read_bytes()


@pytest.mark.safety
def test_already_scored_error_names_manifest_ids_only(tmp_path):
    log = tmp_path / "log.jsonl"
    secret_id = "case: the patient said their address is 12 Elm Road"
    H.record_scoring(log, [secret_id], "rubric-a", at=AT)
    with pytest.raises(H.AlreadyScored) as err:
        H.record_scoring(log, [secret_id], "rubric-a", at=AT)
    assert H.manifest_id(secret_id) in str(err.value)
    assert "Elm Road" not in str(err.value) and "Elm Road" not in log.read_text(encoding="utf-8")


# =============================== HLD-03 exposure scan ===============================


def _leaky_repo(tmp_path: Path) -> Path:
    root = _repo(tmp_path)
    cases = _cases()
    # fx-h1's question, quoted in markdown, re-wrapped and re-cased
    q1 = cases[0]["input"].upper().split()
    (root / "README.md").write_text(
        "# Notes\n\n> " + " ".join(q1[:6]) + "\n> " + " ".join(q1[6:]) + "\n", encoding="utf-8"
    )
    # fx-h2's reference inside a JSON string with an escaped newline
    (root / "data.json").write_text(
        json.dumps({"note": "see:\n" + cases[1]["reference"]}), encoding="utf-8"
    )
    # ten consecutive words of fx-h3's question, split over a Python string concatenation
    words = cases[2]["input"].split()
    (root / "snippet.py").write_text(
        f'X = ("{" ".join(words[1:6])} "\n     "{" ".join(words[6:11])}")\n', encoding="utf-8"
    )
    (root / "clean.txt").write_text(
        "Rivers carry sediment. None of this is held out.\n", encoding="utf-8"
    )
    (root / "image.bin").write_bytes(b"\0" + cases[0]["input"].encode())
    _commit(root, "files")
    (root / "untracked.md").write_text(cases[0]["input"], encoding="utf-8")
    return root


def test_exposure_scan_finds_whole_and_partial_leaks_in_tracked_files_only(tmp_path):
    """logical: normalised whole-string and window matches, tracked files only (a file with a
    NUL byte is scanned too: a leak behind one binary byte is still a leak)."""
    root = _leaky_repo(tmp_path)
    found = H.exposure_scan(_cases(), root)
    assert found == sorted(
        [
            H.Exposure("README.md", H.manifest_id("fx-h1")),
            H.Exposure("data.json", H.manifest_id("fx-h2")),
            H.Exposure("image.bin", H.manifest_id("fx-h1")),
            H.Exposure("snippet.py", H.manifest_id("fx-h3")),
        ]
    )
    assert H.exposure_scan(_cases(), root, window=12) == found[:3]  # 10 words < 12
    assert H.exposure_scan(_cases(), root, min_chars=500, window=50) == []
    assert H.tracked_files(root) == sorted(
        ["README.md", "clean.txt", "data.json", "image.bin", "snippet.py"]
    )


def test_exposure_scan_ignores_short_strings_and_the_id(tmp_path):
    root = _repo(tmp_path)
    (root / "a.txt").write_text("fx-h3 none\nthe answer is none.\n", encoding="utf-8")
    _commit(root, "a")
    assert H.exposure_scan(_cases(), root) == []


def test_exposure_scan_fields_and_public_templates(tmp_path):
    """A shared instruction template or a shared canary is not a leak; a whole held-out string
    copied into a public case still is (found on EvalKit's own agent tasks)."""
    template = "answer in one sentence and round every figure to two decimal places please"
    canary = "CANARY-7f3a-shared-token-for-every-task"
    held = [
        {"id": "h1", "goal": f"Which depot shipped the most crates in May? {template}",
         "canaries": [canary]},
        {"id": "h2", "goal": "Name the supplier whose invoices were late three months running"},
    ]  # fmt: skip
    public = [{"id": "p1", "goal": f"How many lorries left the yard on Monday? {template}"}]
    root = _repo(tmp_path)
    (root / "p1.yaml").write_text(yaml.safe_dump(public[0]), encoding="utf-8")
    (root / "db.sql").write_text(f"INSERT INTO settings VALUES ('{canary}');\n", encoding="utf-8")
    _commit(root, "public")
    noisy = H.exposure_scan(held, root)
    assert {e.path for e in noisy} == {"p1.yaml", "db.sql"}
    assert H.exposure_scan(held, root, fields=["goal"], public=public) == []
    (root / "p2.yaml").write_text(
        yaml.safe_dump({"id": "p2", "goal": held[1]["goal"]}), encoding="utf-8"
    )
    _commit(root, "a held-out goal copied into a public task")
    copied = H.exposure_scan(held, root, fields=["goal"], public=[*public, {"g": held[1]["goal"]}])
    assert copied == [H.Exposure("p2.yaml", H.manifest_id("h2"))]


def test_tracked_files_refuses_a_non_git_folder(tmp_path):
    with pytest.raises(H.HeldoutError, match="git"):
        H.tracked_files(tmp_path)


def test_exposure_scan_output_is_sorted_and_stable(tmp_path):
    """repro: same verdict, same order, on a fresh clone and with the cases shuffled."""
    root = _leaky_repo(tmp_path)
    first = H.exposure_scan(_cases(), root)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(root), str(clone))
    shuffled = _cases()[::-1]
    assert H.exposure_scan(shuffled, clone) == first
    assert first == sorted(first)


@pytest.mark.contract
def test_scan_cli_exit_codes_and_output_format(tmp_path, capsys):
    root = _leaky_repo(tmp_path)
    assert H.main(["scan", "--cases", str(CASES), "--root", str(root)]) == 1
    out = capsys.readouterr().out.splitlines()
    assert all(len(line.split("\t")) == 2 for line in out[:-1])
    clean = _repo(tmp_path, "clean")
    (clean / "a.txt").write_text("nothing\n", encoding="utf-8")
    _commit(clean, "a")
    assert H.main(["scan", "--cases", str(CASES), "--root", str(clean)]) == 0
    assert H.main(["scan", "--cases", str(tmp_path / "no.jsonl"), "--root", str(clean)]) == 2
    assert H.main(["scan", "--cases", str(CASES), "--root", str(tmp_path / "x")]) == 2


@pytest.mark.golden
def test_scan_cli_output_on_the_fixture_repo_is_exact(tmp_path, capsys):
    root = _leaky_repo(tmp_path)
    assert H.main(["scan", "--cases", str(CASES), "--root", str(root)]) == 1
    expected = (
        f"README.md\t{_sha('fx-h1')[:12]}\n"
        f"data.json\t{_sha('fx-h2')[:12]}\n"
        f"image.bin\t{_sha('fx-h1')[:12]}\n"
        f"snippet.py\t{_sha('fx-h3')[:12]}\n"
        "exposed: 3 held-out case(s) in 4 of 5 tracked file(s)\n"
    )
    assert capsys.readouterr().out == expected


@pytest.mark.safety
def test_scan_never_prints_heldout_text(tmp_path, capsys):
    root = _leaky_repo(tmp_path)
    H.main(["scan", "--cases", str(CASES), "--root", str(root)])
    captured = capsys.readouterr()
    both = (captured.out + captured.err).casefold()
    assert captured.out.count("\t") == 4
    for t in _texts():
        assert t.casefold() not in both
        if len(t.split()) >= 4:
            assert " ".join(t.casefold().split()[:4]) not in both
    assert "fx-h" not in both


# =============================== HLD-04 tamper-evident log ===============================


def test_chain_links_each_line_to_the_one_before(tmp_path):
    log = tmp_path / "log.jsonl"
    rows = [H.append_entry(log, _entry(i)) for i in range(3)]
    lines = log.read_text(encoding="utf-8").splitlines()
    assert rows[0]["prev"] == H.GENESIS
    assert rows[1]["prev"] == _sha(lines[0]) and rows[2]["prev"] == _sha(lines[1])
    assert H.chain_problems(log) == []
    assert H.read_log(log) == rows
    assert H.read_log(tmp_path / "absent.jsonl") == []
    with pytest.raises(H.HeldoutError, match="prev"):
        H.append_entry(log, {"kind": "x", "prev": "0" * 64})


def test_tampering_a_middle_line_breaks_the_chain(tmp_path):
    """logical: edited, dropped, inserted and reordered lines are each caught."""
    log = tmp_path / "log.jsonl"
    for i in range(4):
        H.append_entry(log, _entry(i))
    good = log.read_text(encoding="utf-8").splitlines()

    def problems(lines: list[str]) -> list[str]:
        log.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return H.chain_problems(log)

    edited = good[:]
    edited[1] = edited[1].replace('"n":1', '"n":9')
    assert problems(edited) == ["log line 3 does not follow line 2 (edited, dropped or reordered)"]
    assert problems(good[:1] + good[2:]) != []
    assert problems([good[0], good[2], good[1], good[3]]) != []
    assert problems([*good[:2], json.dumps({"kind": "x", "prev": H.GENESIS}), *good[2:]]) != []
    assert problems([*good[:2], "not json", *good[2:]])[0] == "log line 3 is not a JSON object"
    assert problems(good) == []
    log.write_text("\n".join(edited) + "\n", encoding="utf-8")
    with pytest.raises(H.HeldoutError, match="chain"):
        H.append_entry(log, _entry(5))


def test_append_after_a_file_without_trailing_newline_keeps_lines_apart(tmp_path):
    log = tmp_path / "log.jsonl"
    H.append_entry(log, _entry(0))
    log.write_text(log.read_text(encoding="utf-8").rstrip("\n"), encoding="utf-8")
    H.append_entry(log, _entry(1))
    assert H.chain_problems(log) == [] and len(H.read_log(log)) == 2


def test_append_only_passes_for_appends_and_fails_for_a_working_rewrite(tmp_path):
    root = _repo(tmp_path)
    log = root / "logs" / "tuning.jsonl"
    log.parent.mkdir()
    H.append_entry(log, _entry(0))
    base = _commit(root, "one")
    H.append_entry(log, _entry(1))
    _commit(root, "two")
    H.append_entry(log, _entry(2))  # uncommitted append
    assert H.append_only_problems(log, [base]) == []
    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text(lines[0].replace('"n":0', '"n":7') + "\n", encoding="utf-8")
    out = H.append_only_problems(log, [base])
    assert any("HEAD" in p for p in out) and any(base[:12] in p for p in out)


def test_rewritten_history_fails_append_only_even_when_restored(tmp_path):
    """logical: a commit inside base..HEAD that rewrote lines is caught though HEAD looks fine,
    and a history rewrite that replaced the pushed base is caught against that base."""
    root = _repo(tmp_path)
    log = root / "log.jsonl"
    H.append_entry(log, _entry(0))
    H.append_entry(log, _entry(1))
    base = _commit(root, "base")
    good = log.read_text(encoding="utf-8")
    log.write_text(good.replace('"n":0', '"n":5'), encoding="utf-8")
    bad = _commit(root, "rewrite")
    log.write_text(good, encoding="utf-8")
    H.append_entry(log, _entry(2))
    _commit(root, "restore and append")
    assert H.chain_problems(log) == []
    out = H.append_only_problems(log, [base])
    assert any(bad[:12] in p and "commit" in p for p in out)

    # force-push style rewrite: reset below the pushed base, then commit a different log
    _git(root, "reset", "-q", "--hard", base + "~0")
    _git(root, "checkout", "-q", "--orphan", "rewritten")
    log.write_text("", encoding="utf-8")
    H.append_entry(log, {"kind": "note", "n": 42})
    _commit(root, "rewritten history")
    out = H.append_only_problems(log, [base])
    assert any(base[:12] in p for p in out)


def test_merge_that_drops_lines_fails_and_append_after_a_merge_passes(tmp_path):
    """logical: every merge parent's log must survive; a later append on top is fine."""
    root = _repo(tmp_path)
    log = root / "log.jsonl"
    H.append_entry(log, _entry(0))
    base = _commit(root, "base")
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "code.py").write_text("x = 1\n", encoding="utf-8")
    _commit(root, "feature: code only")
    _git(root, "checkout", "-q", "main")
    H.append_entry(log, _entry(1))
    _commit(root, "main appends")
    _git(root, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    merge_parents = _git(root, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    assert len(merge_parents) == 2
    H.append_entry(log, _entry(2))  # append after the merge
    _commit(root, "append after merge")
    assert H.append_only_problems(log, [base, *merge_parents], default_tip="main") == []

    # two branches that both appended: whatever the resolution, one parent's line is lost
    _git(root, "checkout", "-q", "-b", "a")
    H.append_entry(log, {"kind": "note", "n": "a"})
    _commit(root, "a appends")
    _git(root, "checkout", "-q", "main")
    _git(root, "checkout", "-q", "-b", "b")
    H.append_entry(log, {"kind": "note", "n": "b"})
    head_b = _commit(root, "b appends")
    fork = _git(root, "merge-base", "a", "b")
    a_line = _git(root, "show", "a:log.jsonl").splitlines()[-1]
    _git(root, "merge", "-q", "a", check=False)  # conflicts: both appended a line
    assert (root / ".git" / "MERGE_HEAD").is_file()
    resolved = _git(root, "show", f"{head_b}:log.jsonl") + "\n" + a_line + "\n"
    log.write_text(resolved, encoding="utf-8")
    merge = _commit(root, "resolve by keeping both")
    parents = _git(root, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    out = H.append_only_problems(log, [fork, *parents])
    assert H.chain_problems(log) != []  # a's line does not follow b's
    assert any(merge[:12] in p and "merge" in p for p in out), (out, merge, parents)


def test_default_tip_agrees_on_common_length_only(tmp_path):
    root = _repo(tmp_path)
    log = root / "log.jsonl"
    H.append_entry(log, _entry(0))
    _commit(root, "base")
    _git(root, "checkout", "-q", "-b", "feature")
    _git(root, "checkout", "-q", "main")
    H.append_entry(log, _entry(1))
    _commit(root, "main moves ahead")
    _git(root, "checkout", "-q", "feature")
    assert H.append_only_problems(log, [], default_tip="main") == []  # behind is fine
    log.write_text(
        log.read_text(encoding="utf-8") + json.dumps({"kind": "x"}) + "\n", encoding="utf-8"
    )
    out = H.append_only_problems(log, [], default_tip="main")
    assert out == ["the log disagrees with main from line 2"]


def test_missing_revision_or_repository_fails_closed(tmp_path):
    root = _repo(tmp_path)
    log = root / "log.jsonl"
    H.append_entry(log, _entry(0))
    _commit(root, "one")
    out = H.append_only_problems(log, ["0123456789abcdef0123456789abcdef01234567"])
    assert out == ["revision 0123456789ab is not in this clone (fetch the full history)"]
    assert H.append_only_problems(log, [], default_tip="nope") == [
        "revision nope is not in this clone (fetch the full history)"
    ]
    loose = tmp_path / "loose.jsonl"
    H.append_entry(loose, _entry(0))
    assert H.append_only_problems(loose, ["HEAD"]) == [
        "no git repository: the append-only check cannot run"
    ]


def test_ci_revisions_lists_base_merge_parents_and_tip(tmp_path):
    root = _repo(tmp_path)
    log = root / "log.jsonl"
    H.append_entry(log, _entry(0))
    first = _commit(root, "one")
    _git(root, "checkout", "-q", "-b", "feature")
    (root / "f.txt").write_text("f\n", encoding="utf-8")
    feat = _commit(root, "feature")
    _git(root, "checkout", "-q", "main")
    (root / "m.txt").write_text("m\n", encoding="utf-8")
    main_head = _commit(root, "main")
    _git(root, "checkout", "-q", "feature")
    revs, tip = H.ci_revisions(root, base="0" * 40, default_branch="main")
    assert revs == [first] and tip == "main"  # new branch: merge-base, then HEAD's parent
    assert H.ci_revisions(root, base="", default_branch="main") == ([first], "main")
    _git(root, "checkout", "-q", "main")
    _git(root, "merge", "-q", "--no-ff", "-m", "merge", "feature")
    revs, tip = H.ci_revisions(root, base=first, default_branch="main")
    assert revs == [first, main_head, feat] and tip == "main"  # push base, both merge parents
    assert H.ci_revisions(root) == ([main_head, feat], None)
    # a default branch that is not in the clone is still returned, so the check fails closed
    assert H.ci_revisions(root, default_branch="nope") == ([main_head, feat], "nope")


@pytest.mark.contract
def test_verify_log_cli_exit_codes(tmp_path, capsys, monkeypatch):
    root = _repo(tmp_path)
    log = root / "log.jsonl"
    H.append_entry(log, _entry(0))
    base = _commit(root, "one")
    H.append_entry(log, _entry(1))
    _commit(root, "two")
    monkeypatch.chdir(root)
    assert H.main(["verify-log", str(log), "--base", base, "--default-branch", "main"]) == 0
    assert "log: OK" in capsys.readouterr().out
    log.write_text(log.read_text(encoding="utf-8").replace('"n":0', '"n":3'), encoding="utf-8")
    assert H.main(["verify-log", str(log), "--rev", base]) == 1
    assert H.main(["verify-log", str(tmp_path / "missing.jsonl")]) == 2


@pytest.mark.contract
def test_manifest_and_verify_cli(tmp_path, capsys):
    out = tmp_path / "M.json"
    args = ["--split", f"heldout={CASES}", "--split", f"public={PUBLIC}"]
    assert H.main(["manifest", *args, "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    assert (
        "heldout: 3" in printed
        and json.loads(out.read_text(encoding="utf-8"))["manifest_sha256"] in printed
    )
    assert H.main(["verify", "--manifest", str(out), *args]) == 0
    assert H.main(["verify", "--manifest", str(out), "--split", f"heldout={CASES}"]) == 1
    assert H.main(["verify", "--manifest", str(out), "--split", "bad-arg"]) == 2
    assert H.main(["manifest", "--split", f"heldout={tmp_path / 'x'}", "--out", str(out)]) == 2


def test_is_certified_needs_exact_case_ids_and_rubric(tmp_path):
    log = tmp_path / "log.jsonl"
    H.record_scoring(log, ["fx-h1", "fx-h2"], "rubric-a", at=AT)
    H.append_entry(log, {"kind": "note", "rubric_hash": "rubric-z", "case_id_sha256": []})
    assert H.is_certified(log, ["fx-h2", "fx-h1"], "rubric-a")
    assert not H.is_certified(log, ["fx-h1"], "rubric-a")  # a subset is another set
    assert not H.is_certified(log, ["fx-h1", "fx-h2", "fx-h3"], "rubric-a")
    assert not H.is_certified(log, ["fx-h1", "fx-h2"], "rubric-b")
    assert not H.is_certified(log, [], "rubric-z")


@pytest.mark.golden
def test_committed_log_fixture_verifies_and_certifies():
    assert H.chain_problems(GOLDEN_LOG) == []
    assert H.is_certified(GOLDEN_LOG, ["fx-h1", "fx-h2"], "rubric-a")
    assert H.is_certified(GOLDEN_LOG, ["fx-h3"], "rubric-a")
    assert not H.is_certified(GOLDEN_LOG, ["fx-h1", "fx-h2", "fx-h3"], "rubric-a")
    lines = GOLDEN_LOG.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[1])["prev"] == _sha(lines[0])


@pytest.mark.safety
def test_is_certified_fails_closed_on_broken_chain_or_missing_log(tmp_path):
    log = tmp_path / "log.jsonl"
    assert not H.is_certified(log, ["fx-h1"], "rubric-a")
    H.record_scoring(log, ["fx-h0"], "rubric-a", at=AT)
    H.record_scoring(log, ["fx-h1"], "rubric-a", at=AT)
    assert H.is_certified(log, ["fx-h1"], "rubric-a")
    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text(
        lines[1] + "\n", encoding="utf-8"
    )  # drop the first line: the certifying line no longer follows
    assert not H.is_certified(log, ["fx-h1"], "rubric-a")
    forged = json.loads(lines[1])
    forged["rubric_hash"] = "rubric-b"
    log.write_text(lines[0] + "\n" + json.dumps(forged, sort_keys=True) + "\n", encoding="utf-8")
    assert H.chain_problems(log) == []  # prev still names line 1 ...
    assert not H.is_certified(log, ["fx-h1"], "rubric-a")  # ... but the line no longer matches
    log.write_text("garbage\n", encoding="utf-8")
    assert not H.is_certified(log, ["fx-h1"], "rubric-a")


def test_append_only_verdict_is_the_same_on_a_fresh_clone(tmp_path):
    """repro: CI checks a fresh clone; it must reach the same verdict as the working copy."""
    root = _repo(tmp_path)
    log = root / "log.jsonl"
    H.append_entry(log, _entry(0))
    base = _commit(root, "base")
    good = log.read_text(encoding="utf-8")
    log.write_text(good.replace('"n":0', '"n":5'), encoding="utf-8")
    _commit(root, "rewrite")
    log.write_text(good, encoding="utf-8")
    _commit(root, "restore")
    here = H.append_only_problems(log, [base])
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(root), str(clone))
    there = H.append_only_problems(clone / "log.jsonl", [base])
    assert here == there and here != []
    # no explicit cleanup: pytest removes tmp_path, and on Windows git's read-only object files
    # make a plain shutil.rmtree fail (CI 724db3a)


# =============================== verifier findings (B5.14 review) ===============================


def _raw_append(log: Path, row: dict) -> None:
    """Append a line by hand with a correct `prev`, bypassing every library guard."""
    lines = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    row = {**row, "prev": _sha(lines[-1]) if lines else H.GENESIS}
    with log.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")


def test_log_check_enforces_score_once_on_the_log_itself(tmp_path, capsys):
    """HLD-02 on the log: a second scoring of a case under the same (normalised) rubric is a
    problem for chain_problems and verify-log, and append_entry refuses to write it."""
    root = _repo(tmp_path)
    log = root / "log.jsonl"
    first = H.record_scoring(log, ["fx-h1", "fx-h2"], "rubric-a", at=AT)
    base = _commit(root, "one")
    again = {k: v for k, v in first.items() if k != "prev"}
    with pytest.raises(H.AlreadyScored):
        H.append_entry(log, again)
    narrower = {**again, "rubric_hash": " RUBRIC-A ", "case_id_sha256": [_sha("fx-h2")]}
    with pytest.raises(H.AlreadyScored):
        H.append_entry(log, narrower)
    with pytest.raises(H.HeldoutError, match="malformed"):
        H.append_entry(log, {"kind": "heldout_scored", "rubric_hash": "r", "case_id_sha256": [{}]})
    assert H.chain_problems(log) == []
    _raw_append(log, narrower)
    problems = H.chain_problems(log)
    assert len(problems) == 1 and "line 2" in problems[0] and H.manifest_id("fx-h2") in problems[0]
    assert "fx-h2" not in problems[0]
    _commit(root, "dup")
    assert H.main(["verify-log", str(log), "--rev", base]) == 1
    assert "already scored" in capsys.readouterr().out
    assert not H.is_certified(log, ["fx-h1", "fx-h2"], "rubric-a")  # fail closed on a bad log
    with pytest.raises(H.HeldoutError):
        H.record_scoring(log, ["fx-h3"], "rubric-a", at=AT)
    other = tmp_path / "malformed.jsonl"
    _raw_append(other, {"kind": "heldout_scored", "rubric_hash": 3, "case_id_sha256": "x"})
    assert "malformed" in H.chain_problems(other)[0]


def _case_insensitive(folder: Path) -> bool:
    probe = folder / "case-probe.txt"
    probe.write_text("x", encoding="utf-8")
    try:
        return (folder / "CASE-PROBE.TXT").exists()
    finally:
        probe.unlink()


def test_append_only_resolves_the_exact_tracked_path_and_fails_closed(tmp_path, monkeypatch):
    """A differently-cased path (case-insensitive file systems), an untracked log, or a git
    read failure is never treated as an empty committed log."""
    root = _repo(tmp_path)
    log = root / "log.jsonl"
    for i in range(3):
        H.append_entry(log, _entry(i))
    first = _commit(root, "three lines")
    full = log.read_text(encoding="utf-8")
    log.write_text(full.splitlines()[0] + "\n", encoding="utf-8")  # truncated tail
    assert any("HEAD" in p for p in H.append_only_problems(log, [first]))
    if _case_insensitive(root):
        out = H.append_only_problems(root / "LOG.JSONL", [first])
        assert any("HEAD" in p for p in out), out
    log.write_text(full, encoding="utf-8")
    assert H.append_only_problems(log, [first]) == []
    loose = root / "untracked.jsonl"
    H.append_entry(loose, _entry(0))
    assert any("not tracked at HEAD" in p for p in H.append_only_problems(loose, []))
    real_run = H._Git.run

    def broken_read(self, *args):
        if "cat-file" in args:
            return subprocess.CompletedProcess(["git", *args], 128, b"", b"fatal: corrupt")
        return real_run(self, *args)

    monkeypatch.setattr(H._Git, "run", broken_read)
    out = H.append_only_problems(log, [first])
    assert out and all("cannot read" in p for p in out), out


def test_shallow_clone_fails_closed_with_a_fetch_hint(tmp_path, capsys):
    root = _repo(tmp_path)
    log = root / "log.jsonl"
    for i in range(3):
        H.append_entry(log, _entry(i))
        _commit(root, f"c{i}")
    _git(root, "checkout", "-q", "-b", "feature")
    H.append_entry(log, _entry(3))
    feature = _commit(root, "feature appends")
    _git(root, "checkout", "-q", "main")
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", "--no-single-branch", root.as_uri(), str(shallow))
    clog = shallow / "log.jsonl"
    with pytest.raises(H.HeldoutError, match="fetch-depth: 0"):
        H.ci_revisions(shallow, base="0" * 40, default_branch="main")
    # the walk feature..HEAD reaches a commit whose parents the shallow clone cut off
    out = H.append_only_problems(clog, [feature])
    assert any("shallow" in p and "fetch-depth: 0" in p for p in out), out
    rc = H.main(["verify-log", str(clog), "--base", "0" * 40, "--default-branch", "main"])
    assert rc != 0 and "fetch-depth: 0" in capsys.readouterr().err
    full = tmp_path / "full"
    _git(tmp_path, "clone", "-q", root.as_uri(), str(full))
    revs, tip = H.ci_revisions(full, base="0" * 40, default_branch="main")
    assert H.append_only_problems(full / "log.jsonl", revs, default_tip=tip) == []


_SCORE_CHILD = """
import sys, time
from pathlib import Path
from evalkit import heldout as H
log, go = Path(sys.argv[1]), Path(sys.argv[2])
while not go.exists():
    time.sleep(0.001)
try:
    H.record_scoring(log, ["fx-h1"], "rubric-a", at="2026-11-17")
except H.AlreadyScored:
    sys.exit(3)
"""


def test_two_processes_cannot_both_score_a_case(tmp_path):
    """HLD-02 under concurrency: read-check-append holds an exclusive lock on the log."""
    for round_ in range(3):
        log = tmp_path / f"log{round_}.jsonl"
        go = tmp_path / f"go{round_}"
        H.record_scoring(log, ["seed"], "rubric-0", at=AT)
        procs = [
            subprocess.Popen([sys.executable, "-c", _SCORE_CHILD, str(log), str(go)])
            for _ in range(6)
        ]
        go.write_text("go", encoding="utf-8")
        codes = sorted(p.wait(timeout=120) for p in procs)
        assert codes == [0, 3, 3, 3, 3, 3], codes
        assert H.chain_problems(log) == [] and len(H.read_log(log)) == 2


def test_a_held_log_lock_blocks_another_writer(tmp_path):
    log = tmp_path / "log.jsonl"
    H.record_scoring(log, ["seed"], "rubric-0", at=AT)
    go = tmp_path / "go"
    go.write_text("go", encoding="utf-8")
    with H._log_lock(log):
        child = subprocess.Popen([sys.executable, "-c", _SCORE_CHILD, str(log), str(go)])
        time.sleep(1.5)
        assert child.poll() is None  # waiting for the lock
        assert len(H.read_log(log)) == 1
    assert child.wait(timeout=60) == 0
    assert len(H.read_log(log)) == 2 and H.chain_problems(log) == []


def test_rubric_hashes_are_normalised_and_case_ids_must_be_a_collection(tmp_path):
    log = tmp_path / "log.jsonl"
    row = H.record_scoring(log, ["fx-h1"], "  ABCDEF0123  ", at=AT)
    assert row["rubric_hash"] == "abcdef0123"
    for variant in ("abcdef0123", "ABCDEF0123", " abcdef0123\n"):
        with pytest.raises(H.AlreadyScored):
            H.record_scoring(log, ["fx-h1"], variant, at=AT)
        assert H.scored_before(log, ["fx-h1"], variant) == [H.manifest_id("fx-h1")]
        assert H.is_certified(log, ["fx-h1"], variant)
    for bad in ("has space", "x" * 129, "-leading", "émoji", 7, None):
        with pytest.raises(H.HeldoutError, match="rubric"):
            H.record_scoring(log, ["fx-h2"], bad, at=AT)  # type: ignore[arg-type]
        assert not H.is_certified(log, ["fx-h1"], bad)  # type: ignore[arg-type]
    H.record_scoring(log, ["fx-h2"], "sha256:" + "a" * 64, at=AT)
    with pytest.raises(H.HeldoutError, match="case_ids"):
        H.record_scoring(log, "fx-h3", "rubric-a", at=AT)
    with pytest.raises(H.HeldoutError, match="case_ids"):
        H.scored_before(log, "fx-h1", "abcdef0123")
    with pytest.raises(H.HeldoutError, match="case id"):
        H.record_scoring(log, [["fx-h3"]], "rubric-a", at=AT)  # type: ignore[list-item]
    assert not H.is_certified(log, "fx-h1", "abcdef0123")
    assert len(H.read_log(log)) == 2


def _secret_repo(tmp_path: Path, files: dict[str, bytes | str]) -> Path:
    root = _repo(tmp_path, "secrets")
    for name, data in files.items():
        p = root / name
        if isinstance(data, bytes):
            p.write_bytes(data)
        else:
            p.write_text(data, encoding="utf-8")
    _commit(root, "files")
    return root


_SYNTH = "The quarterly zebra migration doesn't follow the river past Ndutu in March"
_SYNTH_CASES = [{"id": "s1", "q": _SYNTH}]


def test_exposure_scan_sees_html_escapes_hyphenation_nfkc_and_binary_files(tmp_path):
    wide = _SYNTH.translate({ord(c): ord(c) + 0xFEE0 for c in "zebra"})  # fullwidth letters
    files: dict[str, bytes | str] = {
        "html.html": "<p>" + _SYNTH.replace("'", "&#39;").replace(" ", "&nbsp;") + "</p>",
        "hyphen.md": _SYNTH.replace("migration", "migra-\ntion").replace("river", "ri-\n> ver"),
        "zero_byte.txt": _SYNTH.encode() + b"\0\x89PNG",  # not "nul.txt": reserved on Windows
        "utf16.txt": _SYNTH.encode("utf-16"),
        "wide.txt": wide,
        "clean.txt": "Nothing private here at all, only a note about rivers and zebras.",
    }
    root = _secret_repo(tmp_path, files)
    found = {e.path for e in H.exposure_scan(_SYNTH_CASES, root)}
    assert found == {"html.html", "hyphen.md", "zero_byte.txt", "utf16.txt", "wide.txt"}, found


def test_exposure_scan_of_the_index_reads_staged_content(tmp_path, capsys):
    root = _secret_repo(tmp_path, {"a.txt": "clean\n", "b.txt": _SYNTH})
    (root / "a.txt").write_text(_SYNTH, encoding="utf-8")
    _git(root, "add", "a.txt")
    (root / "a.txt").write_text("clean again\n", encoding="utf-8")  # staged leak, clean file
    (root / "b.txt").write_text("cleaned in the work tree only\n", encoding="utf-8")
    sid = H.manifest_id("s1")
    assert H.exposure_scan(_SYNTH_CASES, root) == []
    assert H.exposure_scan(_SYNTH_CASES, root, index=True) == [
        H.Exposure("a.txt", sid),
        H.Exposure("b.txt", sid),
    ]
    cases = tmp_path / "s.jsonl"
    cases.write_text(json.dumps(_SYNTH_CASES[0]) + "\n", encoding="utf-8")
    assert H.main(["scan", "--cases", str(cases), "--root", str(root)]) == 0
    assert H.main(["scan", "--cases", str(cases), "--root", str(root), "--staged"]) == 1
    out = capsys.readouterr().out
    assert f"a.txt\t{sid}" in out and "zebra" not in out.casefold()


@pytest.mark.safety
def test_is_certified_never_raises(tmp_path, monkeypatch):
    log = tmp_path / "log.jsonl"
    H.record_scoring(log, ["fx-h1"], "rubric-a", at=AT)
    assert H.is_certified(log, ["fx-h1"], "rubric-a")
    assert not H.is_certified(log, None, "rubric-a")  # type: ignore[arg-type]
    assert not H.is_certified(log, [{"x": 1}], "rubric-a")  # type: ignore[list-item]
    assert not H.is_certified(log, ["fx-h1"], ["rubric-a"])  # type: ignore[arg-type]
    assert not H.is_certified(tmp_path, ["fx-h1"], "rubric-a")  # a folder
    assert not H.is_certified(123, ["fx-h1"], "rubric-a")  # type: ignore[arg-type]
    real = Path.read_bytes

    def unreadable(self):
        if self.name == "log.jsonl":
            raise PermissionError("denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", unreadable)
    assert not H.is_certified(log, ["fx-h1"], "rubric-a")


def test_malformed_cases_raise_heldout_error_and_ids_are_nfc(tmp_path, capsys):
    secret = "a private question that must not be echoed"
    mixed = tmp_path / "mixed.yaml"
    mixed.write_text(f"- {{id: a, 1: x, q: '{secret}'}}\n", encoding="utf-8")
    loop = tmp_path / "loop.yaml"
    loop.write_text(f"- &c {{id: b, q: '{secret}', self: *c}}\n", encoding="utf-8")
    for bad in (mixed, loop):
        with pytest.raises(H.HeldoutError) as err:
            H.load_cases(bad)
        assert bad.name in str(err.value) and secret not in str(err.value)
        out = tmp_path / "M.json"
        assert H.main(["manifest", "--split", f"h={bad}", "--out", str(out)]) == 2
        captured = capsys.readouterr()
        assert "Traceback" not in captured.err and secret not in captured.out + captured.err
    with pytest.raises(H.HeldoutError):
        H.content_hash({"id": "a", 1: "x", "q": "y"})  # type: ignore[dict-item]
    nfc, nfd = unicodedata.normalize("NFC", "café"), unicodedata.normalize("NFD", "café")
    assert nfc != nfd and H.case_id_hash(nfd) == H.case_id_hash(nfc) == _sha(nfc)
    dup = tmp_path / "dup.jsonl"
    dup.write_text(
        json.dumps({"id": nfc, "q": "x"}) + "\n" + json.dumps({"id": nfd, "q": "y"}) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(H.HeldoutError, match="duplicate"):
        H.build_manifest({"h": dup})
    log = tmp_path / "log.jsonl"
    H.record_scoring(log, [nfc], "rubric-a", at=AT)
    with pytest.raises(H.AlreadyScored):
        H.record_scoring(log, [nfd], "rubric-a", at=AT)
    with pytest.raises(H.HeldoutError):
        H.exposure_scan(["not a mapping"], tmp_path)  # type: ignore[list-item]


# =============================== verifier pass 2 (B5.14) ===============================


def test_scan_with_a_field_no_case_has_is_an_error_not_a_pass(tmp_path):
    """A misspelt or renamed --field used to scan nothing and report "no held-out text"."""
    cases = [{"id": "c1", "goal": "find the quarterly revenue of the northern region stores"}]
    with pytest.raises(H.HeldoutError, match="gaol"):
        H.exposure_scan(cases, tmp_path, fields=["gaol"])


def test_deeply_nested_or_unreadable_case_files_are_heldout_errors(tmp_path):
    deep = tmp_path / "deep.json"
    deep.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
    with pytest.raises(H.HeldoutError):
        H.load_cases(deep)
    deep_l = tmp_path / "deep.jsonl"
    deep_l.write_text("[" * 200_000 + "]" * 200_000 + "\n", encoding="utf-8")
    with pytest.raises(H.HeldoutError):
        H.load_cases(deep_l)


def test_exposure_scan_reads_held_out_text_in_keys_too(tmp_path):
    """B5.11 verifier: held-out text used as a mapping key was skipped by the scan."""
    secret_key = "the quarterly zebra migration crosses the northern river at dawn"
    cases = [{"id": "k1", "planted": {secret_key: "x"}}]
    root = _secret_repo(tmp_path, {"leak.txt": f"note: {secret_key}\n"})
    found = {e.path for e in H.exposure_scan(cases, root)}
    assert found == {"leak.txt"}


def test_an_accented_copy_of_heldout_text_is_still_found():
    """B5.7 verifier pass 3: combining accents after letters hid held-out text."""
    assert H._normalise("Cafe\u0301 de\u0301ja\u0300 vu") == H._normalise("Cafe deja vu")
    assert H._normalise("na\u00efve") == H._normalise("naive")
