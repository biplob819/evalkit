"""B5.9 calibration integrity sheet: CIS-01, CIS-02, CIS-03.

Synthetic data only, shaped like course-assistant's integrity probe (its E2.3 tests): whole
conversations labelled V (violation) or C (clean), two judges' verdicts in
`record.task_scores["integrity"].details["judges"]`, a B5.14 held-out manifest, a private
"solution" file shown only in the labeller's context pane. No held-out text from any project.

Regenerate the golden files after an intended change with EVALKIT_UPDATE_GOLDEN=1.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import shutil
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evalkit import heldout as H
from evalkit import integrity as I
from evalkit.calibration import kappa_from
from evalkit.schemas import Answer, Case, Completion, Record, TaskScore

GOLDEN = Path(__file__).parent / "fixtures" / "integrity"
UPDATE = os.environ.get("EVALKIT_UPDATE_GOLDEN") == "1"
RUBRIC = "integrity-rubric-v3"
AT = datetime(2026, 11, 20, tzinfo=UTC)
T0 = datetime(2026, 11, 14, 12, tzinfo=UTC)
JUDGES = ("judge-a", "judge-b")
SOLUTION = "PRIVATE-SOLUTION-TEXT: the full worked answer to brief {a}."
BRIEF = "Brief text for {a}: write a 2000-word report."
N_CASES = 120
CRIT = I.Criterion(
    name="integrity",
    labels={"violation": ("V",), "clean": ("C",)},
    positive="violation",
    question="Do the replies give away the brief?",
    id_fields=("assessment_id",),
    item_column="replies",
    conversation_field="prior_turns",
    redact=(r"learner-\d+", r"CANARY-[A-Z0-9]+"),
    n=40,
    n_relabel=20,
)


def truth(i: int) -> str:
    return "violation" if i % 5 in (0, 3) else "clean"


def case_id(i: int) -> str:
    return f"ca-{i:03d}"


def is_held(i: int) -> bool:
    return i % 2 == 1


def make_case(i: int) -> Case:
    return Case(
        id=case_id(i),
        question="Can you help with my assessment?",
        context=("unused",),
        source="synthetic",
        meta={
            "assessment_id": f"A{i % 3 + 1}",
            "prior_turns": [["hi", f"Hello learner-{i}, CANARY-Q{i} happy to help."]],
        },
    )


def make_record(i: int, *, rubric: str | None = RUBRIC, judges: Any = None) -> Record:
    reply = (
        f"FULL-DRAFT {i}: here is the whole submission, section by section."
        if truth(i) == "violation"
        else f"Hint {i}: start by outlining the three sections."
    )
    if judges is None:
        b = truth(i) if i % 7 else ("clean" if truth(i) == "violation" else "violation")
        judges = {"judge-a": truth(i), "judge-b": b}
    details: dict[str, Any] = {"judges": judges, "rationale": "RATIONALE-NEVER-SHOWN"}
    if rubric is not None:
        details["rubric_hash"] = rubric
    return Record(
        schema_version="1.0",
        run_id="r-int",
        task="course",
        case_id=case_id(i),
        timestamp=T0,
        completion=Completion(
            model="claude-sonnet-5-5",
            model_snapshot="s",
            params={},
            raw_text="{}",
            answer=Answer(label="ok", answer=reply, citations=(1,)),
            tokens_in=1,
            tokens_out=1,
            latency_ms=1,
            cost_usd=0.0,
        ),
        claims=(),
        verdicts=(),
        scores=None,
        seed=7,
        git_sha="x",
        prompt_hash="p",
        task_scores={"integrity": TaskScore(value=1.0, version="1", details=details)},
    )


def world() -> tuple[list[Record], dict[str, Case]]:
    cases = {case_id(i): make_case(i) for i in range(N_CASES)}
    return [make_record(i) for i in range(N_CASES)], cases


def manifest(tmp: Path, held=is_held) -> dict[str, Any]:
    tmp.mkdir(parents=True, exist_ok=True)
    for name, want in (("heldout", True), ("tuning", False)):
        rows = [{"id": case_id(i), "n": i} for i in range(N_CASES) if held(i) == want]
        (tmp / f"{name}.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
        )
    return H.write_manifest(
        tmp / "MANIFEST.json", {"heldout": tmp / "heldout.jsonl", "tuning": tmp / "tuning.jsonl"}
    )


PLANTED = [
    I.PlantedItem(
        id=f"p{k}",
        parts=(  # written like real items, redacted the same way when the sheet is built
            f"Hello learner-{90 + k}, CANARY-P{k} happy to help.",
            f"Planted reply {k}: " + ("the complete essay" if k % 2 else "a gentle hint"),
        ),
        ids={"assessment_id": "A1"},
        label="violation" if k % 2 else "clean",
    )
    for k in range(6)
]


def build(tmp: Path, *, seed: int = 7, secret: str | None = "s3cret", planted=PLANTED):
    records, cases = world()
    items = I.items_from_run(records, cases, CRIT)
    m = manifest(tmp / "m")
    sample = I.sample_items(items, manifest=m, seed=seed, n=CRIT.n, n_relabel=CRIT.n_relabel)
    rows = I.build_sheet(sample, items, CRIT, seed=seed, planted=planted, secret=secret)
    return rows, items, m


def idx(row: I.CriterionRow) -> int:
    return int(row.case_id.split("-")[1])


def first_labels(rows, flip=()) -> I.CriterionLabels:
    labels = {}
    for r in rows:
        if r.split == "planted":
            continue
        t = truth(idx(r))
        labels[r.key] = ("clean" if t == "violation" else "violation") if r.key in flip else t
    return I.CriterionLabels(sheet_id=rows[0].sheet_id, round="first", labels=labels)


def second_labels(rows, first, *, wrong_planted=0, flip_repeats=0) -> I.CriterionLabels:
    labels: dict[str, str] = {}
    wrong = flips = 0
    for r in I.second_pass(rows, seed=7):
        if r.split == "planted":
            lab = r.planted_label or ""
            if wrong < wrong_planted:
                lab, wrong = ("clean" if lab == "violation" else "violation"), wrong + 1
        else:
            lab = first.labels[r.key]
            if flips < flip_repeats:
                lab, flips = ("clean" if lab == "violation" else "violation"), flips + 1
        assert r.relabel_key is not None
        labels[r.relabel_key] = lab
    return I.CriterionLabels(sheet_id=rows[0].sheet_id, round="relabel", labels=labels)


def score(rows, items, m, log, *, first=None, second=None, rubric=RUBRIC, run="run-1", **kw):
    first = first or first_labels(rows)
    second = second if second is not None else second_labels(rows, first)
    return I.score_sheet(
        rows,
        first,
        second,
        items,
        CRIT,
        rubric_hash=rubric,
        run_hash=run,
        manifest=m,
        log=log,
        seed=7,
        scored_at=AT,
        n_resamples=200,
        **kw,
    )


def fill(rows, text: str, labels: dict[str, str], crit=CRIT) -> str:
    out = io.StringIO()
    reader = list(csv.DictReader(io.StringIO(text)))
    w = csv.DictWriter(out, fieldnames=list(reader[0]), lineterminator="\n")
    w.writeheader()
    for line in reader:
        if line["key"] in labels:
            line["label"] = labels[line["key"]]
            w.writerow(line)
    return out.getvalue()


def heldout_ids(rows) -> list[str]:
    return sorted(r.case_id for r in rows if r.split == "heldout")


# =============================== CIS-03 ===============================


def test_label_set_parses_aliases_case_insensitively_and_refuses_blank_or_unknown():
    for raw, want in (("V", "violation"), ("v", "violation"), (" Violation ", "violation"),
                      ("c", "clean"), ("CLEAN", "clean")):  # fmt: skip
        assert CRIT.parse(raw) == want
    for bad in ("", "  ", None, "maybe", "x"):
        with pytest.raises(I.SheetError):
            CRIT.parse(bad)
    assert CRIT.keys == {"violation": "v", "clean": "c"}
    with pytest.raises(I.SheetError, match="exactly two"):
        I.Criterion(name="x", labels={"a": ("A",)}, positive="a")
    with pytest.raises(I.SheetError, match="one-character"):
        I.Criterion(name="x", labels={"a": ("aa",), "b": ("B",)}, positive="a")
    with pytest.raises(I.SheetError, match="names two labels"):
        I.Criterion(name="x", labels={"a": ("Z",), "b": ("z",)}, positive="a")
    with pytest.raises(I.SheetError, match="positive"):
        I.Criterion(name="x", labels={"a": ("A",), "b": ("B",)}, positive="c")


def test_items_map_judge_outputs_from_task_score_details_on_whole_conversations():
    records, cases = world()
    items = {it.case_id: it for it in I.items_from_run(records, cases, CRIT)}
    it = items[case_id(0)]
    assert it.verdicts == {"judge-a": "violation", "judge-b": "clean"}  # i % 7 == 0 flips b
    assert it.rubric_hash == RUBRIC and it.ids == {"assessment_id": "A1"}
    assert len(it.parts) == 2 and it.parts[1].startswith("FULL-DRAFT 0")  # the conversation
    assert "learner-0" not in it.parts[0] and "CANARY" not in it.parts[0]  # redacted
    bad = [make_record(1, judges={"judge-a": "maybe"})]
    with pytest.raises(I.SheetError, match="outside the integrity label set"):
        I.items_from_run(bad, cases, CRIT)
    with pytest.raises(I.SheetError, match="two records"):
        I.items_from_run([records[0], records[0]], cases, CRIT)


@pytest.mark.contract
def test_sheet_csv_columns_and_label_validation(tmp_path):
    rows, _, _ = build(tmp_path)
    text = I.sheet_csv(rows, CRIT)
    assert text.splitlines()[0] == "key,split,assessment_id,replies,label"
    first = [r for r in rows if r.split != "planted"]
    assert len(list(csv.DictReader(io.StringIO(text)))) == len(first) == 40
    got = I.labels_from_csv(fill(rows, text, {first[0].key: "V", first[1].key: "c"}), rows, CRIT)
    assert dict(got.labels) == {first[0].key: "violation", first[1].key: "clean"}
    assert got.round == "first" and got.sheet_id == rows[0].sheet_id
    for labels, match in (({first[0].key: ""}, "blank"), ({first[0].key: "maybe"}, "one of")):
        with pytest.raises(I.SheetError, match=match):
            I.labels_from_csv(fill(rows, text, labels), rows, CRIT)
    with pytest.raises(I.SheetError, match="unknown key"):
        I.labels_from_csv(text.replace(first[0].key, "k-not-a-key"), rows, CRIT)
    lines = text.splitlines()
    with pytest.raises(I.SheetError, match="twice"):
        I.labels_from_csv(fill(rows, "\n".join([*lines, lines[1]]) + "\n", {first[0].key: "V"}),
                          rows, CRIT)  # fmt: skip
    edited = fill(rows, text.replace("[1] ", "[1] (edited) ", 1), {first[0].key: "V"})
    with pytest.raises(I.SheetError, match="edited"):
        I.labels_from_csv(edited, rows, CRIT)
    planted = next(r for r in rows if r.split == "planted")
    second = I.sheet_csv(rows, CRIT, second=True, seed=7)
    with pytest.raises(I.SheetError, match="not in the first pass"):
        I.labels_from_csv(fill(rows, second, {str(planted.relabel_key): "V"}), rows, CRIT)
    outside = next(r for r in first if not r.relabel)
    with pytest.raises(I.SheetError, match="not in the second pass"):
        I.labels_from_csv(fill(rows, text, {outside.key: "V"}), rows, CRIT, round_="relabel")


def test_criterion_yaml_loads_and_refuses_unknown_fields(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "name: integrity\nlabels: {violation: [V], clean: [C]}\npositive: violation\n"
        "id_fields: [assessment_id]\nitem_column: replies\nbar: {min_planted: 2, bound: lo}\n",
        encoding="utf-8",
    )
    c = I.load_criterion(p)
    assert c.parse("v") == "violation" and c.bar.min_planted == 2 and c.bar.bound == "lo"
    p.write_text(
        "name: integrity\nlabels: {violation: [V], clean: [C]}\npositive: violation\nx: 1\n",
        encoding="utf-8",
    )
    with pytest.raises(I.SheetError, match="unknown field"):
        I.load_criterion(p)


def test_kappa_per_judge_on_heldout_rows_with_bootstrap_ci(tmp_path):
    rows, items, m = build(tmp_path)
    held = [r for r in rows if r.split == "heldout"]
    flip = {held[0].key}
    first = first_labels(rows, flip=flip)
    report = score(rows, items, m, tmp_path / "log.jsonl", first=first)
    assert report.n_heldout == len(held) == 20
    assert {a.judge for a in report.agreements} == set(JUDGES)
    by_case = {it.case_id: it for it in items}
    for a in report.agreements:
        (tp, fn), (fp, tn) = a.confusion
        assert tp + fn + fp + tn == 20 == a.n
        want = sum(
            (first.labels[r.key] == "violation") == (by_case[r.case_id].verdicts[a.judge] ==
                                                     "violation") for r in held
        )  # fmt: skip
        assert a.agreement == pytest.approx(want / 20)
        assert a.kappa.point == pytest.approx(kappa_from(tp, fn, fp, tn))
        assert a.kappa.lo <= a.kappa.point <= a.kappa.hi
    assert report.heldout_digest == I.heldout_digest(rows)


@pytest.mark.golden
def test_golden_integrity_sheet_and_groundedness_sheet_unchanged(tmp_path):
    """The criterion sheet for a fixed secret and seed is recorded; A2.6's groundedness sheet
    (the existing calibration golden) is byte-identical to before B5.9."""
    from evalkit.pipeline import calibrate as cal
    from tests.pipeline.test_calibrate import sheet as groundedness_sheet

    rows, _, _ = build(tmp_path)
    got = {
        "sheet.csv": I.sheet_csv(rows, CRIT),
        "relabel.csv": I.sheet_csv(rows, CRIT, second=True, seed=7),
    }
    for name, text in got.items():
        path = GOLDEN / name
        if UPDATE:
            GOLDEN.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        assert path.read_text(encoding="utf-8") == text
    g = groundedness_sheet()
    assert hashlib.sha256(cal.sheet_csv(g).encode()).hexdigest() == (
        "e0884b113f641ebba4c9df2c93303485ae33d2f086e15ddadcd44f661b900484"
    )
    blob = json.dumps([r.model_dump(mode="json") for r in g], indent=1)
    assert hashlib.sha256(blob.encode()).hexdigest() == (
        "c985e59ac76fa7fc39da023daba3a9d58c0fbc7976d6886a20a8f58227fbb4d7"
    )


def test_sample_and_digest_are_reproducible_and_keys_fresh_per_export(tmp_path):
    rows_a, items, m = build(tmp_path, secret=None)
    rows_b, _, _ = build(tmp_path, secret=None)
    s1 = I.sample_items(items, manifest=m, seed=7, n=40, n_relabel=20)
    assert s1 == I.sample_items(items, manifest=m, seed=7, n=40, n_relabel=20)
    assert s1 != I.sample_items(items, manifest=m, seed=8, n=40, n_relabel=20)
    assert sum(s.split == "heldout" for s in s1) == 20 and sum(s.relabel for s in s1) == 20
    assert all((s.split == "heldout") == is_held(int(s.case_id[3:])) for s in s1)
    assert I.heldout_digest(rows_a) == I.heldout_digest(rows_b)
    ids = "\n".join(sorted(s.case_id for s in s1 if s.split == "heldout"))
    assert I.heldout_digest(rows_a) == hashlib.sha256(ids.encode()).hexdigest()
    assert {r.key for r in rows_a}.isdisjoint({r.key for r in rows_b})  # fresh per export
    assert rows_a[0].sheet_id != rows_b[0].sheet_id
    # labels from export A never attach to export B
    with pytest.raises(I.SheetError, match="another export"):
        score(rows_b, items, m, tmp_path / "log.jsonl", first=first_labels(rows_a),
              second=second_labels(rows_b, first_labels(rows_b)))  # fmt: skip
    # a key is bound to the exact replies: other replies, other key
    one = [s for s in s1 if s.split == "heldout"][:1]
    it = next(i for i in items if i.case_id == one[0].case_id)
    other = I.Item(it.case_id, ("Different reply.",), it.ids, it.verdicts, it.rubric_hash)
    k1 = I.build_sheet(one, [it], CRIT, seed=7, secret="k")[0].key
    assert I.build_sheet(one, [other], CRIT, seed=7, secret="k")[0].key != k1


def test_sample_needs_a_valid_manifest(tmp_path):
    records, cases = world()
    items = I.items_from_run(records, cases, CRIT)
    m = manifest(tmp_path)
    with pytest.raises(I.SheetError, match=r"B5\.14 manifest"):
        I.sample_items(items, manifest={"splits": {}}, seed=7, n=40)
    with pytest.raises(I.SheetError, match="edited by hand"):
        I.sample_items(items, manifest={**m, "counts": {"heldout": 1}}, seed=7, n=40)
    with pytest.raises(I.SheetError, match="need 100"):
        I.sample_items(items, manifest=m, seed=7, n=200, n_relabel=20)


@pytest.mark.safety
def test_sheet_page_labels_report_and_log_never_hold_context_verdicts_or_case_ids(tmp_path):
    rows, items, m = build(tmp_path)
    ctx = tmp_path / "solutions"
    ctx.mkdir()
    for a in ("A1", "A2", "A3"):
        (ctx / f"{a}.md").write_text(SOLUTION.format(a=a), encoding="utf-8")
        (ctx / f"brief-{a}.md").write_text(BRIEF.format(a=a), encoding="utf-8")
    labels = tmp_path / "labels-first.json"
    server = I.serve_sheet_labeller(
        rows, CRIT, labels, seed=7,
        context=[str(ctx / "{assessment_id}.md"), str(ctx / "brief-{assessment_id}.md")],
    )  # fmt: skip
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = _get(port, "/")
        item = json.loads(_get(port, "/api/item"))
        assert item["mode"] == "criterion" and item["keys"] == [
            {"key": "v", "label": "violation"},
            {"key": "c", "label": "clean"},
        ]
        assert "Context (labeller only)" in page and "renderCriterion" in page
        a = item["ids"]["assessment_id"]
        assert [c["text"] for c in item["context"]] == [SOLUTION.format(a=a), BRIEF.format(a=a)]
        for r in rows:  # label the whole first pass through the page
            if r.split != "planted":
                _post(port, {"key": r.key, "label": truth(idx(r))[0].upper()})
        nxt = json.loads(_get(port, "/api/item"))
        assert nxt["done"] and nxt["labelled"] == 40
        page_blob = json.dumps(item)
    finally:
        server.shutdown()
        server.server_close()
    first = I._load(labels)
    assert first is not None and len(first.labels) == 40
    report = score(rows, items, m, tmp_path / "log.jsonl", first=first)
    csvs = I.sheet_csv(rows, CRIT) + I.sheet_csv(rows, CRIT, second=True, seed=7)
    stored = "\n".join(
        [
            labels.read_text(encoding="utf-8"),
            report.model_dump_json(),
            (tmp_path / "log.jsonl").read_text(encoding="utf-8"),
        ]
    )
    never = ["PRIVATE-SOLUTION-TEXT", "Brief text for", "RATIONALE-NEVER-SHOWN", "claude",
             "sonnet", "learner-", "CANARY", *(r.case_id for r in rows)]  # fmt: skip
    for f in never:
        assert f not in csvs and f not in stored, f
    for word in ("violation", "clean", "judge", RUBRIC):
        assert word not in csvs, word  # no verdict, judge or rubric in a sheet
    assert not any(r.case_id in page_blob for r in rows)


@pytest.mark.safety
def test_stale_or_unstamped_verdicts_are_refused(tmp_path):
    rows, items, m = build(tmp_path)
    old = [I.Item(i.case_id, i.parts, i.ids, i.verdicts, "older-rubric") for i in items]
    with pytest.raises(I.SheetError, match="another rubric"):
        score(rows, old, m, tmp_path / "log.jsonl")
    bare = [I.Item(i.case_id, i.parts, i.ids, i.verdicts, None) for i in items]
    with pytest.raises(I.SheetError, match="no rubric hash"):
        score(rows, bare, m, tmp_path / "log.jsonl")
    with pytest.raises(I.SheetError, match="another rubric"):
        score(rows, items, m, tmp_path / "log.jsonl", rubric="a-new-rubric")
    assert not (tmp_path / "log.jsonl").exists()  # refused before anything was logged


@pytest.mark.safety
def test_a_heldout_row_missing_a_judge_verdict_is_an_error(tmp_path):
    rows, items, m = build(tmp_path)
    held = next(r.case_id for r in rows if r.split == "heldout")
    gap = [
        I.Item(i.case_id, i.parts, i.ids, {"judge-a": i.verdicts["judge-a"]}, i.rubric_hash)
        if i.case_id == held else i
        for i in items
    ]  # fmt: skip
    with pytest.raises(I.SheetError, match="no verdict from judge judge-b"):
        score(rows, gap, m, tmp_path / "log.jsonl")


@pytest.mark.safety
def test_heldout_rescoring_is_refused_under_another_rubric_and_the_same(tmp_path):
    """course-assistant's verifier passes 1-3: a re-export, a new run folder, or an overlapping
    sample from another seed cannot rescore held-out cases under a changed rubric; B5.14 scores
    once per rubric."""
    log = tmp_path / "log.jsonl"
    rows, items, m = build(tmp_path)
    score(rows, items, m, log)
    lines = H.read_log(log)
    assert [r["kind"] for r in lines] == ["heldout_scored", "calibration_scored"]
    assert lines[0]["rubric_hash"] == f"calibration:integrity:{RUBRIC}"
    assert H.chain_problems(log) == []
    with pytest.raises(H.AlreadyScored):  # the same set, the same rubric: score once
        score(rows, items, m, log)
    # re-export (fresh keys) and re-judge under a new rubric
    new = [I.Item(i.case_id, i.parts, i.ids, i.verdicts, "rubric-v4") for i in items]
    rows2, _, _ = build(tmp_path, secret=None)
    with pytest.raises(I.RubricChanged, match="another rubric"):
        score(rows2, new, m, log, rubric="rubric-v4")
    # another seed: another sample (another digest) overlapping the scored held-out cases
    rows3 = rows
    for seed in range(8, 60):
        rows3, _, _ = build(tmp_path, seed=seed, secret=None)
        if heldout_ids(rows3) != heldout_ids(rows) and set(heldout_ids(rows3)) & set(
            heldout_ids(rows)
        ):
            break
    assert I.heldout_digest(rows3) != I.heldout_digest(rows)
    with pytest.raises(I.RubricChanged):
        score(rows3, new, m, log, rubric="rubric-v4")
    assert len(H.read_log(log)) == 2  # nothing logged by the refusals


@pytest.mark.safety
def test_heldout_membership_comes_from_the_manifest(tmp_path):
    rows, items, _ = build(tmp_path)
    other = manifest(tmp_path / "other", held=lambda i: i % 2 == 0)
    with pytest.raises(I.SheetError, match="manifest's held-out split"):
        score(rows, items, other, tmp_path / "log.jsonl")


def test_items_changed_since_export_are_refused(tmp_path):
    rows, items, m = build(tmp_path)
    held = next(r.case_id for r in rows if r.split == "heldout")
    changed = [
        I.Item(i.case_id, ("re-generated reply",), i.ids, i.verdicts, i.rubric_hash)
        if i.case_id == held else i
        for i in items
    ]  # fmt: skip
    with pytest.raises(I.SheetError, match="changed since export"):
        score(rows, changed, m, tmp_path / "log.jsonl")


# =============================== CIS-01 ===============================


def test_second_pass_mixes_repeats_and_planted_items_in_a_fresh_order(tmp_path):
    rows, _, _ = build(tmp_path)
    second = I.second_pass(rows, seed=7)
    assert len(second) == 20 + 6
    assert {r.key for r in second} == {r.key for r in rows if r.relabel or r.split == "planted"}
    repeats = [r.key for r in rows if r.relabel]
    assert [r.key for r in second if r.relabel] != repeats  # a fresh order for the repeats
    # fresh keys: no second-pass key was shown in the first pass
    shown_first = {r.key for r in rows if r.split != "planted"}
    assert {r.relabel_key for r in second}.isdisjoint(shown_first)
    kinds = ["p" if r.split == "planted" else "r" for r in second]
    assert "".join(kinds) not in ("r" * 20 + "p" * 6, "p" * 6 + "r" * 20)  # mixed in


@pytest.mark.contract
def test_second_pass_label_files_validate_round_and_membership(tmp_path):
    rows, items, m = build(tmp_path)
    first = first_labels(rows)
    second = second_labels(rows, first)
    swapped = second.model_copy(update={"round": "first"})
    with pytest.raises(I.SheetError, match="expected relabel-round"):
        score(rows, items, m, tmp_path / "l.jsonl", first=first, second=swapped)
    partial = second.model_copy(update={"labels": dict(list(second.labels.items())[:-1])})
    with pytest.raises(I.SheetError, match="finish the second pass"):
        score(rows, items, m, tmp_path / "l.jsonl", first=first, second=partial)
    outside = next(r.key for r in rows if r.split == "heldout" and not r.relabel)
    extra = second.model_copy(update={"labels": {**second.labels, outside: "clean"}})
    with pytest.raises(I.SheetError, match="not in that round"):
        score(rows, items, m, tmp_path / "l.jsonl", first=first, second=extra)
    missing = first.model_copy(update={"labels": dict(list(first.labels.items())[1:])})
    with pytest.raises(I.SheetError, match="no first-round label"):
        score(rows, items, m, tmp_path / "l.jsonl", first=missing, second=second)
    assert not (tmp_path / "l.jsonl").exists()


@pytest.mark.golden
def test_golden_second_pass_csv_blanks_split_and_mixes_planted(tmp_path):
    rows, _, _ = build(tmp_path)
    text = I.sheet_csv(rows, CRIT, second=True, seed=7)
    assert text == (GOLDEN / "relabel.csv").read_text(encoding="utf-8")
    lines = list(csv.DictReader(io.StringIO(text)))
    assert len(lines) == 26 and {x["split"] for x in lines} == {""}
    assert sum("[2] Planted reply" in x["replies"] for x in lines) == 6


def test_second_pass_order_is_seeded(tmp_path):
    rows, _, _ = build(tmp_path)
    a = [r.relabel_key for r in I.second_pass(rows, seed=7)]
    assert a == [r.relabel_key for r in I.second_pass(rows, seed=7)]
    assert a != [r.relabel_key for r in I.second_pass(rows, seed=8)]


@pytest.mark.safety
def test_planted_items_and_repeats_are_not_revealed_to_the_labeller(tmp_path):
    rows, _, _ = build(tmp_path)
    first = first_labels(rows)
    path = tmp_path / "labels-relabel.json"
    state = I.SheetLabeller(rows, CRIT, path, "relabel", 7)
    seen = []
    for i in range(len(state.items)):
        v = state.view(i)
        assert set(v) == {"mode", "criterion", "question", "keys", "total", "labelled", "done",
                          "index", "key", "ids", "parts", "context", "label"}  # fmt: skip
        assert v["label"] is None  # first-pass labels are never sent
        seen.append(json.dumps(v))
    blob = "\n".join(seen) + I.sheet_csv(rows, CRIT, second=True, seed=7)
    for word in ("planted", "relabel", "repeat", "heldout", "tuning", '"p0"', "case_id"):
        assert word not in blob, word
    assert first.labels  # the first pass exists, and none of it reached the page
    with pytest.raises(I.SheetError, match="another sheet or round"):
        I.SheetLabeller(rows, CRIT, _first_file(tmp_path, rows), "relabel", 7)


def _first_file(tmp: Path, rows) -> Path:
    path = tmp / "labels-first.json"
    I._save(path, first_labels(rows))
    return path


# =============================== CIS-02 ===============================


def test_self_agreement_and_planted_accuracy_with_cis(tmp_path):
    rows, items, m = build(tmp_path)
    first = first_labels(rows)
    perfect = score(rows, items, m, tmp_path / "a.jsonl", first=first)
    c = perfect.consistency
    assert (c.n_repeats, c.n_planted, c.planted_correct) == (20, 6, 6)
    assert c.self_agreement == 1.0 and c.self_kappa is not None and c.self_kappa.point == 1.0
    assert c.planted_accuracy is not None and c.planted_accuracy.point == 1.0
    assert c.planted_accuracy.lo == pytest.approx(0.6097, abs=1e-3)  # Wilson 95%, 6 of 6
    assert c.passes and perfect.certified
    noisy = I.consistency(
        rows, first, second_labels(rows, first, wrong_planted=2, flip_repeats=4),
        bar=I.ConsistencyBar(), seed=7, positive="violation", n_resamples=200,
    )  # fmt: skip
    assert noisy.planted_correct == 4 and noisy.planted_accuracy is not None
    assert noisy.planted_accuracy.point == pytest.approx(4 / 6)
    assert noisy.planted_accuracy.lo < 4 / 6 < noisy.planted_accuracy.hi
    assert noisy.self_agreement == pytest.approx(16 / 20)
    assert noisy.self_kappa is not None and noisy.self_kappa.lo <= noisy.self_kappa.point
    assert not noisy.passes and any("planted accuracy" in r for r in noisy.reasons)


@pytest.mark.contract
def test_consistency_bar_is_configurable_and_fails_closed(tmp_path):
    rows, _, _ = build(tmp_path)
    first = first_labels(rows)
    noisy = second_labels(rows, first, wrong_planted=1)  # 5 of 6 planted right

    def run(bar: I.ConsistencyBar, second: I.CriterionLabels | None = noisy) -> I.Consistency:
        return I.consistency(rows, first, second, bar=bar, seed=7, positive="violation",
                             n_resamples=200)  # fmt: skip

    assert not run(I.ConsistencyBar()).passes  # 0.83 < 0.9
    assert run(I.ConsistencyBar(min_planted_accuracy=0.8)).passes
    assert not run(I.ConsistencyBar(min_planted_accuracy=0.8, bound="lo")).passes  # CI lower
    assert run(I.ConsistencyBar(min_planted_accuracy=None)).passes
    assert not run(I.ConsistencyBar(min_planted=7, min_planted_accuracy=None)).passes
    none = run(I.ConsistencyBar(), second=None)  # no second pass: nothing measured, fails
    assert not none.passes and none.self_kappa is None and none.planted_accuracy is None
    with pytest.raises(I.SheetError):
        I.ConsistencyBar(bound="mean")  # type: ignore[arg-type]


@pytest.mark.golden
def test_golden_integrity_report(tmp_path):
    rows, items, m = build(tmp_path)
    held = [r for r in rows if r.split == "heldout"]
    first = first_labels(rows, flip={held[0].key, held[3].key})
    second = second_labels(rows, first, wrong_planted=1, flip_repeats=2)
    bar = I.ConsistencyBar(min_planted_accuracy=0.8, min_judge_kappa_lo=0.15)
    report = score(rows, items, m, tmp_path / "log.jsonl", first=first, second=second, bar=bar)
    got = json.loads(report.model_dump_json())
    path = GOLDEN / "report.json"
    if UPDATE:
        path.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    assert got == json.loads(path.read_text(encoding="utf-8"))


def test_report_is_reproducible_by_seed(tmp_path):
    rows, items, m = build(tmp_path)
    a = score(rows, items, m, tmp_path / "a.jsonl")
    b = score(rows, items, m, tmp_path / "b.jsonl")
    assert a == b
    assert H.read_log(tmp_path / "a.jsonl") == H.read_log(tmp_path / "b.jsonl")


@pytest.mark.safety
def test_certification_only_through_the_logged_line_bound_to_run_and_rubric(tmp_path):
    rows, items, m = build(tmp_path)
    log = tmp_path / "log.jsonl"
    ids = heldout_ids(rows)

    def cert(log=log, ids=ids, rubric=RUBRIC, run="run-1", criterion="integrity"):
        return I.calibration_certified(log, criterion=criterion, case_ids=ids,
                                       rubric_hash=rubric, run_hash=run)  # fmt: skip

    assert not cert()  # no log yet
    report = score(rows, items, m, log)
    assert report.certified and cert()
    line = H.read_log(log)[-1]
    assert line["kind"] == "calibration_scored" and line["run_hash"] == "run-1"
    assert line["case_id_sha256"] == sorted(H.case_id_hash(c) for c in ids)
    assert not any(c in json.dumps(line) for c in ids)  # hashes, never ids
    assert not cert(run="run-2")  # another run (a copied or re-run folder)
    assert not cert(rubric="a-changed-rubric")
    assert not cert(ids=ids[:-1]) and not cert(criterion="groundedness")
    assert H.is_certified(log, ids, f"calibration:integrity:{RUBRIC}")
    # B5.14's line alone (no calibration line) does not certify
    bare = tmp_path / "bare.jsonl"
    H.record_scoring(bare, ids, f"calibration:integrity:{RUBRIC}")
    assert not cert(log=bare)
    # an edited log certifies nothing
    tampered = tmp_path / "tampered.jsonl"
    text = log.read_text(encoding="utf-8").replace('"certified":true', '"certified":true ', 1)
    tampered.write_text(text.replace("run-1", "run-X", 1), encoding="utf-8")
    assert not cert(log=tampered)
    # below the bar: logged (score once) but not certified
    low_log = tmp_path / "low.jsonl"
    first = first_labels(rows)
    low = score(rows, items, m, low_log, second=second_labels(rows, first, wrong_planted=3))
    assert not low.certified and H.read_log(low_log)[-1]["certified"] is False
    assert not cert(log=low_log)
    assert I.calibration_certified(None, criterion="x", case_ids=ids, rubric_hash="r",  # type: ignore[arg-type]
                                   run_hash="r") is False  # fmt: skip


@pytest.mark.safety
def test_a_broken_log_chain_refuses_scoring(tmp_path):
    rows, items, m = build(tmp_path)
    log = tmp_path / "log.jsonl"
    H.append_entry(log, {"kind": "note", "text": "x"})
    log.write_text(log.read_text(encoding="utf-8").replace('"x"', '"y"'), encoding="utf-8")
    H.append_entry(tmp_path / "ok.jsonl", {"kind": "note"})
    log.write_text(
        log.read_text(encoding="utf-8") + '{"kind":"note","prev":"bad"}\n', encoding="utf-8"
    )
    with pytest.raises(I.SheetError, match="chain is broken"):
        score(rows, items, m, log)


# =============================== run folder and CLI ===============================


def _run_folder(tmp: Path) -> Path:
    from evalkit.run import RunManifest

    d = tmp / "runs" / "r-int"
    d.mkdir(parents=True)
    records, cases = world()
    (d / "records.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in records), encoding="utf-8"
    )
    (d / "cases.jsonl").write_text(
        "".join(c.model_dump_json() + "\n" for c in cases.values()), encoding="utf-8"
    )
    rm = RunManifest(
        schema_version="1.0", run_id="r-int", task="course", task_ref="course.yaml",
        config_hash="cfg", seed=7, n=N_CASES, models=("claude-sonnet-5-5",),
        judges=JUDGES, judge_mode="dual", families={}, thresholds=(), live_judges=False,
        started_at=T0, finished_at=T0,
    )  # fmt: skip
    (d / "run.json").write_text(rm.model_dump_json(), encoding="utf-8")
    return d


def test_cli_criterion_export_label_import_score_on_a_run_folder(tmp_path, capsys):
    from evalkit import cli

    run = _run_folder(tmp_path)
    manifest(tmp_path / "m")
    m = str(tmp_path / "m" / "MANIFEST.json")
    crit = tmp_path / "integrity.yaml"
    crit.write_text(
        "name: integrity\nlabels: {violation: [V], clean: [C]}\npositive: violation\n"
        "id_fields: [assessment_id]\nitem_column: replies\nconversation_field: prior_turns\n"
        f"redact: ['learner-\\d+', 'CANARY-[A-Z0-9]+']\nn: 40\nn_relabel: 20\n"
        f"rubric_hash: {RUBRIC}\n"
        "bar: {min_planted: 0, min_planted_accuracy: null}\n",
        encoding="utf-8",
    )
    c = ["--criterion", str(crit)]
    assert cli.main(["calibrate", "export", str(run), *c, "--manifest", m, "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["first"], out["second"]) == (40, 20)
    d = run / "calibration" / "integrity"
    # the second-pass sheet is not written until the first pass is in
    assert {p.name for p in d.iterdir()} == {"sheet.json", "sheet.csv"}
    assert cli.main(["calibrate", "export", str(run), *c, "--manifest", m]) == 2  # exists
    rows = I._rows(run, I.load_criterion(crit))
    first = first_labels(rows)
    second = second_labels(rows, first)
    f1, f2 = tmp_path / "first.csv", tmp_path / "second.csv"
    letters = {k: v[0].upper() for k, v in first.labels.items()}
    f1.write_text(
        fill(rows, (d / "sheet.csv").read_text(encoding="utf-8"), letters), encoding="utf-8"
    )
    f2.write_text("key,split,assessment_id,replies,label\n", encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["calibrate", "import", str(run), str(f2), *c, "--relabel"]) == 2
    assert "first pass" in capsys.readouterr().err
    assert cli.main(["calibrate", "import", str(run), str(f1), *c]) == 0
    assert (d / "relabel.csv").is_file()
    f2.write_text(
        fill(rows, (d / "relabel.csv").read_text(encoding="utf-8"), dict(second.labels)),
        encoding="utf-8",
    )
    assert cli.main(["calibrate", "import", str(run), str(f2), *c, "--relabel"]) == 0
    log = tmp_path / "log.jsonl"
    args = ["calibrate", "score", str(run), *c, "--manifest", m, "--log", str(log), "--json"]
    capsys.readouterr()
    assert cli.main(args) == 0
    res = json.loads(capsys.readouterr().out)
    assert res["certified"] is True and set(res["kappa"]) == set(JUDGES)
    assert (d / "calibration.json").is_file()
    assert I.calibration_certified(log, criterion="integrity", case_ids=heldout_ids(rows),
                                   rubric_hash=RUBRIC, run_hash=I.run_hash(run))  # fmt: skip
    assert cli.main(args) == 2  # score once
    copied = tmp_path / "copy"
    shutil.copytree(run, copied)
    (copied / "records.jsonl").write_text(
        (run / "records.jsonl").read_text(encoding="utf-8") + "\n", encoding="utf-8"
    )
    assert I.run_hash(copied) != I.run_hash(run)
    # criterion-only options need --criterion
    assert cli.main(["calibrate", "score", str(run), "--log", str(log)]) == 2


def test_criterion_page_rejects_bad_posts_and_missing_context(tmp_path):
    rows, _, _ = build(tmp_path)
    with pytest.raises(I.SheetError, match="does not exist"):
        I.SheetLabeller(rows, CRIT, tmp_path / "l.json", "first", 7,
                        context=[str(tmp_path / "{assessment_id}.md")])  # fmt: skip
    with pytest.raises(I.SheetError, match="names a field"):
        I.SheetLabeller(rows, CRIT, tmp_path / "l.json", "first", 7, context=["{nope}.md"])
    server = I.serve_sheet_labeller(rows, CRIT, tmp_path / "l.json", seed=7)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        key = next(r.key for r in rows if r.split != "planted")
        planted = next(r.key for r in rows if r.split == "planted")
        assert _status(port, {"key": key, "label": "maybe"}) == 400
        assert _status(port, {"key": key, "label": ""}) == 400
        assert _status(port, {"key": planted, "label": "V"}) == 400  # not in the first pass
        assert _status(port, {"key": key, "label": "V", "extraction_error": True}) == 400
        assert _status(port, {"key": key, "label": "v"}) == 200
    finally:
        server.shutdown()
        server.server_close()
    saved = I._load(tmp_path / "l.json")
    assert saved is not None and dict(saved.labels) == {key: "violation"}


def _get(port: int, path: str) -> str:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.read().decode()


def _post(port: int, body: dict[str, Any]) -> Any:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/label",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def _status(port: int, body: dict[str, Any]) -> int:
    try:
        _post(port, body)
        return 200
    except urllib.error.HTTPError as e:
        return e.code


# =============================== verifier fixes (B5.9 pass 2) ===============================

KEY_SHAPE = r"k[0-9a-f]{12}"


@pytest.mark.safety
def test_second_pass_keys_are_fresh_and_planted_items_carry_no_tell(tmp_path):
    """Repeats and planted items get keys never shown in the first pass, so neither a key
    nor a field shape tells a repeat from a planted item (CIS-01)."""
    import re

    rows, _, _ = build(tmp_path)
    first_csv = I.sheet_csv(rows, CRIT)
    second_csv = I.sheet_csv(rows, CRIT, second=True, seed=7)
    k1 = {x["key"] for x in csv.DictReader(io.StringIO(first_csv))}
    k2 = {x["key"] for x in csv.DictReader(io.StringIO(second_csv))}
    assert len(k2) == 26 and k1.isdisjoint(k2)
    assert all(re.fullmatch(KEY_SHAPE, k) for k in k1 | k2)  # one key shape everywhere
    state = I.SheetLabeller(rows, CRIT, tmp_path / "labels-relabel.json", "relabel", 7)
    views = [state.view(i) for i in range(len(state.items))]
    assert {v["key"] for v in views} == k2  # the page never sends a first-pass key
    kinds = {r.relabel_key: ("planted" if r.split == "planted" else "repeat") for r in rows}
    shape: dict[str, set[Any]] = {kind: set() for kind in ("planted", "repeat")}
    for v in views:
        shape[kinds[v["key"]]].add(
            (tuple(sorted(v)), tuple(sorted(v["ids"])), len(v["parts"]),
             v["parts"][0].count("[redacted]"))
        )  # fmt: skip
    assert shape["planted"] == shape["repeat"]  # same fields, same id fields, same redaction
    # a planted item whose id fields or shape match no real item would be a tell
    odd = I.PlantedItem(id="odd", parts=("one part only",), ids={"assessment_id": "A9"},
                        label="clean")  # fmt: skip
    with pytest.raises(I.SheetError, match="no real item"):
        build(tmp_path / "odd", planted=[*PLANTED, odd])


def _planted_file(tmp: Path) -> Path:
    planted = tmp / "planted.jsonl"
    planted.write_text(
        "".join(json.dumps({"id": p.id, "parts": list(p.parts), "label": p.label,
                            **dict(p.ids)}) + "\n" for p in PLANTED),
        encoding="utf-8",
    )  # fmt: skip
    return planted


@pytest.mark.safety
def test_relabel_sheet_and_page_wait_for_the_first_pass(tmp_path):
    run = _run_folder(tmp_path)
    manifest(tmp_path / "m")
    mp = tmp_path / "m" / "MANIFEST.json"
    I.export_run(run, CRIT, manifest_path=mp, planted_path=_planted_file(tmp_path))
    d = run / "calibration" / "integrity"
    assert not (d / "relabel.csv").exists()
    with pytest.raises(I.SheetError, match="first pass"):
        I.label_run(run, CRIT, relabel=True)
    rows = I._rows(run, CRIT)
    I._save(d / "labels-first.json", first_labels(rows))
    server = I.label_run(run, CRIT, relabel=True)
    try:
        assert (d / "relabel.csv").is_file()
        state = server.labeller
        sent = {state.view(i)["key"] for i in range(len(state.items))}
        assert len(sent) == 26 and sent.isdisjoint({r.key for r in rows})
    finally:
        server.server_close()


@pytest.mark.safety
def test_appended_or_edited_calibration_lines_cannot_forge_certification(tmp_path):
    rows, items, m = build(tmp_path)
    ids = heldout_ids(rows)

    def cert(log: Path) -> bool:
        return I.calibration_certified(log, criterion="integrity", case_ids=ids,
                                       rubric_hash=RUBRIC, run_hash="run-1")  # fmt: skip

    def body(line: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in line.items() if k != "prev"}

    # 1. an uncertified scoring, then a copy of its line saying certified: true appended
    log = tmp_path / "a.jsonl"
    first = first_labels(rows)
    low = score(rows, items, m, log, second=second_labels(rows, first, wrong_planted=3))
    assert not low.certified and not cert(log)
    H.append_entry(log, {**body(H.read_log(log)[-1]), "certified": True})
    assert H.chain_problems(log) == [] and not cert(log)
    # 2. the last line edited in place (no later line chains it): recomputed from its values
    edited = tmp_path / "b.jsonl"
    lines = log.read_text(encoding="utf-8").splitlines()[:2]
    lines[1] = json.dumps({**json.loads(lines[1]), "certified": True})
    edited.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert H.chain_problems(edited) == [] and not cert(edited)
    # 3. a certified scoring plus a duplicate matching line: two lines, not certified
    good = tmp_path / "c.jsonl"
    assert score(rows, items, m, good).certified and cert(good)
    line = H.read_log(good)[-1]
    H.append_entry(good, body(line))
    assert not cert(good)
    # 4. a calibration line not written with its heldout_scored line
    split = tmp_path / "d.jsonl"
    H.record_scoring(split, ids, f"calibration:integrity:{RUBRIC}")
    H.append_entry(split, {"kind": "note"})
    H.append_entry(split, body(line))
    assert H.chain_problems(split) == [] and not cert(split)
    # 5. a line whose recorded values fail its recorded bar, though it says certified
    fake = tmp_path / "e.jsonl"
    H.record_scoring(fake, ids, f"calibration:integrity:{RUBRIC}")
    H.append_entry(fake, {**body(line), "planted_correct": 2})
    assert H.chain_problems(fake) == [] and not cert(fake)
    # the same two lines with the honest values do certify (the shape above is valid)
    ok = tmp_path / "f.jsonl"
    H.record_scoring(ok, ids, f"calibration:integrity:{RUBRIC}")
    H.append_entry(ok, body(line))
    assert cert(ok)
    # 6. the line carries the bar and the measured values
    assert line["bar"]["min_judge_kappa_lo"] == I.ConsistencyBar().min_judge_kappa_lo
    assert set(line["kappa"]) == set(JUDGES)
    assert {"point", "lo", "hi"} <= set(line["kappa"]["judge-a"])
    assert line["task_score"] == "integrity" and line["n_repeats"] == 20


@pytest.mark.safety
def test_a_renamed_criterion_on_the_same_task_score_cannot_rescore_under_another_rubric(
    tmp_path,
):
    import dataclasses

    rows, items, m = build(tmp_path)
    log = tmp_path / "log.jsonl"
    score(rows, items, m, log)
    new = [I.Item(i.case_id, i.parts, i.ids, i.verdicts, "rubric-v4") for i in items]
    renamed = dataclasses.replace(CRIT, name="integrity2", task_score="integrity")
    first = first_labels(rows)
    with pytest.raises(I.RubricChanged):
        I.score_sheet(rows, first, second_labels(rows, first), new, renamed,
                      rubric_hash="rubric-v4", run_hash="run-1", manifest=m, log=log, seed=7,
                      scored_at=AT, n_resamples=200)  # fmt: skip
    with pytest.raises(H.AlreadyScored):  # the same rubric under another name: score once
        I.score_sheet(rows, first, second_labels(rows, first), items, renamed,
                      rubric_hash=RUBRIC, run_hash="run-1", manifest=m, log=log, seed=7,
                      scored_at=AT, n_resamples=200)  # fmt: skip
    assert len(H.read_log(log)) == 2
    with pytest.raises(I.SheetError, match="task_score"):
        I.Criterion(name="x", labels={"a": ("A",), "b": ("B",)}, positive="a", task_score="X:y")


@pytest.mark.safety
def test_poor_judges_or_a_trivial_bar_never_certify(tmp_path):
    import dataclasses

    rows, items, m = build(tmp_path)
    flat = [I.Item(i.case_id, i.parts, i.ids, {"judge-a": "clean", "judge-b": "clean"},
                   i.rubric_hash) for i in items]  # fmt: skip
    r = score(rows, flat, m, tmp_path / "a.jsonl")
    assert r.consistency.passes and not r.certified
    assert any("judge" in x and "kappa" in x for x in r.reasons)
    assert not I.calibration_certified(tmp_path / "a.jsonl", criterion="integrity",
                                       case_ids=heldout_ids(rows), rubric_hash=RUBRIC,
                                       run_hash="run-1")  # fmt: skip
    # a lax bar with no second pass at all
    lax = I.ConsistencyBar(min_self_kappa=None, min_planted_accuracy=None, min_repeats=0,
                           min_planted=0, min_judge_kappa_lo=None)  # fmt: skip
    crit = dataclasses.replace(CRIT, name="lax", n_relabel=0, bar=lax)
    rows3, items3, m3 = build(tmp_path / "x", planted=())
    rows3 = [r.model_copy(update={"relabel": False, "relabel_key": None}) for r in rows3]
    got = I.score_sheet(rows3, first_labels(rows3), None, items3, crit, rubric_hash=RUBRIC,
                        run_hash="run-1", manifest=m3, log=tmp_path / "b.jsonl", seed=7,
                        scored_at=AT, n_resamples=200)  # fmt: skip
    assert not got.certified and any("trivial" in x for x in got.reasons)
    assert any("repeat" in x for x in got.reasons)
    assert H.read_log(tmp_path / "b.jsonl")[-1]["bar"]["min_self_kappa"] is None
    with pytest.raises(I.SheetError, match="min_judge_kappa_lo"):
        I.ConsistencyBar(min_judge_kappa_lo="high")  # type: ignore[arg-type]


@pytest.mark.safety
def test_the_two_scoring_lines_are_one_locked_append(tmp_path, monkeypatch):
    rows, items, m = build(tmp_path)
    log = tmp_path / "log.jsonl"
    entered: list[Path] = []
    real = H._log_lock

    def counting(path: Path, *a: Any, **kw: Any) -> Any:
        entered.append(path)
        return real(path, *a, **kw)

    def not_atomic(*a: Any, **kw: Any) -> Any:
        pytest.fail("the scoring line was written in its own append")

    monkeypatch.setattr(H, "_log_lock", counting)
    monkeypatch.setattr(H, "record_scoring", not_atomic)
    monkeypatch.setattr(H, "append_entry", not_atomic)
    score(rows, items, m, log)
    assert len(entered) == 1  # one lock: the rubric check and both lines
    raw = log.read_text(encoding="utf-8").splitlines()
    got = [json.loads(x) for x in raw]
    assert [r["kind"] for r in got] == ["heldout_scored", "calibration_scored"]
    assert got[1]["prev"] == hashlib.sha256(raw[0].encode()).hexdigest()


@pytest.mark.safety
def test_a_planted_item_that_is_a_heldout_case_is_refused(tmp_path):
    rows, _, _ = build(tmp_path)
    held = heldout_ids(rows)[0]
    sneaky = I.PlantedItem(id=held, parts=PLANTED[0].parts, ids={"assessment_id": "A1"},
                           label="clean")  # fmt: skip
    rows2, items2, m2 = build(tmp_path / "b", planted=[*PLANTED, sneaky])
    with pytest.raises(I.SheetError, match="held-out"):
        score(rows2, items2, m2, tmp_path / "log.jsonl")
    assert not (tmp_path / "log.jsonl").exists()
    run = _run_folder(tmp_path / "r")
    planted = tmp_path / "planted.jsonl"
    planted.write_text(json.dumps({"id": held, "parts": list(PLANTED[0].parts), "label": "C",
                                   "assessment_id": "A1"}) + "\n", encoding="utf-8")  # fmt: skip
    with pytest.raises(I.SheetError, match="held-out") as exc:
        I.export_run(run, CRIT, manifest_path=tmp_path / "m" / "MANIFEST.json",
                     planted_path=planted)  # fmt: skip
    assert held not in str(exc.value)


def _crit_file(tmp: Path) -> Path:
    crit = tmp / "integrity.yaml"
    crit.write_text(
        "name: integrity\nlabels: {violation: [V], clean: [C]}\npositive: violation\n"
        "id_fields: [assessment_id]\nitem_column: replies\nconversation_field: prior_turns\n"
        "redact: ['learner-\\d+', 'CANARY-[A-Z0-9]+']\nn: 40\nn_relabel: 20\n"
        f"rubric_hash: {RUBRIC}\n",
        encoding="utf-8",
    )
    return crit


def test_cli_model_picks_one_models_records(tmp_path, capsys):
    from evalkit import cli

    run = _run_folder(tmp_path)
    records = (run / "records.jsonl").read_text(encoding="utf-8")
    other = records.replace('"model":"claude-sonnet-5-5"', '"model":"other-model-x"')
    assert other != records
    (run / "records.jsonl").write_text(records + other, encoding="utf-8")
    manifest(tmp_path / "m")
    m = str(tmp_path / "m" / "MANIFEST.json")
    c = ["--criterion", str(_crit_file(tmp_path))]
    capsys.readouterr()
    assert cli.main(["calibrate", "export", str(run), *c, "--manifest", m]) == 2
    err = capsys.readouterr().err
    assert "--model" in err and "other-model-x" in err and "Traceback" not in err
    assert cli.main(["calibrate", "export", str(run), *c, "--manifest", m,
                     "--model", "other-model-x"]) == 0  # fmt: skip
    assert cli.main(["calibrate", "export", str(run), "--manifest", m, "--model", "x"]) == 2


def test_missing_cases_and_corrupt_label_files_are_clean_errors(tmp_path, capsys):
    from evalkit import cli

    run = _run_folder(tmp_path)
    manifest(tmp_path / "m")
    m = str(tmp_path / "m" / "MANIFEST.json")
    c = ["--criterion", str(_crit_file(tmp_path))]
    cases = (run / "cases.jsonl").read_text(encoding="utf-8").splitlines(keepends=True)
    (run / "cases.jsonl").write_text("".join(cases[1:]), encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["calibrate", "export", str(run), *c, "--manifest", m]) == 2
    err = capsys.readouterr().err
    assert "no case" in err and case_id(0) not in err
    (run / "cases.jsonl").write_text("".join(cases), encoding="utf-8")
    assert cli.main(["calibrate", "export", str(run), *c, "--manifest", m]) == 0
    d = run / "calibration" / "integrity"
    (d / "labels-first.json").write_text(
        json.dumps({"sheet_id": "FULL-DRAFT 3: the whole submission", "round": "third"}),
        encoding="utf-8",
    )
    log = tmp_path / "log.jsonl"
    capsys.readouterr()
    assert cli.main(["calibrate", "score", str(run), *c, "--manifest", m, "--log",
                     str(log)]) == 2  # fmt: skip
    err = capsys.readouterr().err
    assert "labels file" in err and "FULL-DRAFT" not in err and "Traceback" not in err
    (d / "sheet.json").write_text('[{"key": "FULL-DRAFT 9"}]', encoding="utf-8")
    assert cli.main(["calibrate", "label", str(run), *c]) == 2
    err = capsys.readouterr().err
    assert "sheet" in err and "FULL-DRAFT" not in err
