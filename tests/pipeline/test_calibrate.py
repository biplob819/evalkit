import itertools
import json
import threading
import urllib.error
import urllib.request
from datetime import UTC, datetime
from typing import Any, Literal

import pytest

from evalkit.pipeline import calibrate as cal
from evalkit.pipeline.aggregate import calibration_status_of
from evalkit.pipeline.judge import BLIND_DENYLIST, JUDGE_PROMPT_HASH
from evalkit.pipeline.labeller import load_labels, serve_labeller
from evalkit.schemas import (
    Answer,
    Case,
    Claim,
    ClaimVerdict,
    Completion,
    Interval,
    Record,
    RunSummary,
)
from evalkit.schemas.calibration import CalibrationLabel

T0 = datetime(2026, 11, 14, 12, tzinfo=UTC)
MODELS = ("claude-sonnet-5-5", "gpt-6.1-sol")
FAMILY = {"claude-sonnet-5-5": "anthropic", "gpt-6.1-sol": "openai"}
LABELS = ("yes", "no", "maybe")


def make_case(i: int) -> Case:
    return Case(
        id=f"pqa-{i}",
        question="q",
        context=(f"Sentence one of case {i}.", f"Claude-Opus wrote sentence two of case {i}."),
        reference_label=LABELS[i % 3],
        source="t",
    )


def make_record(case: Case, model: str, n_claims: int = 3, error: bool = False) -> Record:
    claims = (
        ()
        if error
        else tuple(
            Claim(
                id=f"{case.id}:{model}:{k}",
                text=f"Claim {k} from {model} about {case.id}.",
                cited=(1 + k % 2,),
                extractor_version="e",
            )
            for k in range(n_claims)
        )
    )
    comp = Completion(
        model=model,
        model_snapshot="s",
        params={},
        raw_text="{}",
        answer=None if error else Answer(label="yes", answer="a", citations=(1,)),
        tokens_in=1,
        tokens_out=1,
        latency_ms=1,
        cost_usd=0.0,
        error="transport" if error else None,
    )
    return Record(
        schema_version="1.0",
        run_id="r",
        task="pubmedqa",
        case_id=case.id,
        timestamp=T0,
        completion=comp,
        claims=claims,
        verdicts=(),
        scores=None,
        seed=7,
        git_sha="x",
        prompt_hash="p",
    )


def world(n_cases=90):
    cases = {c.id: c for c in (make_case(i) for i in range(n_cases))}
    records = [
        make_record(c, m, error=(i == 0)) for i, c in enumerate(cases.values()) for m in MODELS
    ]
    return cases, records


# ---- CAL-01 sample ---------------------------------------------------------------------------


def test_sample_is_stratified_split_and_reproducible():
    cases, records = world()
    s = cal.export_sample(records, cases, seed=7)
    assert len(s) == 100 and len({x.record_key for x in s}) == 100
    tuning = [x for x in s if x.split == "tuning"]
    held = [x for x in s if x.split == "heldout"]
    assert len(tuning) == len(held) == 50
    assert {x.record_key for x in tuning}.isdisjoint({x.record_key for x in held})
    relabel = [x for x in s if x.relabel]
    assert len(relabel) == 20 and sum(x.split == "tuning" for x in relabel) == 10
    assert all(x.record_key != "pqa-0:" + m for x in s for m in MODELS)  # errored: no claims
    # strata (model, label) are proportional within one answer
    labellable = [r for r in records if r.claims]
    for m in MODELS:
        for lab in LABELS:
            pool = sum(
                1
                for r in labellable
                if r.completion.model == m and cases[r.case_id].reference_label == lab
            )
            got = sum(1 for x in s if x.model == m and cases[x.case_id].reference_label == lab)
            assert abs(got - 100 * pool / len(labellable)) <= 1
    assert cal.export_sample(records, cases, seed=7) == s
    assert cal.export_sample(records, cases, seed=8) != s


def test_sample_refuses_too_few_answers():
    cases, records = world(20)
    with pytest.raises(ValueError, match="labellable"):
        cal.export_sample(records, cases, seed=7)


# ---- CAL-02 blind sheet, CSV and the labelling page -------------------------------------------


def lf(rows, labels, round_: Literal["first", "relabel"] = "first"):
    return cal.LabelFile(sheet_id=rows[0].sheet_id, round=round_, labels=labels)


def sheet():
    cases, records = world()
    sample = cal.export_sample(records, cases, seed=7)
    return cal.build_sheet(sample, records, cases, seed=7, secret="test-secret")


@pytest.mark.safety
def test_sheet_and_csv_are_blind():
    rows = sheet()
    assert len(rows) == 300
    csv_text = cal.sheet_csv(rows)
    lowered = csv_text.casefold()
    for name in ("claude-sonnet", "gpt-6.1", "claude-opus", ":claude", ":gpt"):
        assert name not in lowered
    assert "[model] wrote sentence two" in csv_text  # cited sentences are redacted too
    assert all(r.key.startswith("k") and r.answer.startswith("a") for r in rows)


def fill(rows, label=lambda r: "S"):
    import csv
    import io

    reader = csv.DictReader(io.StringIO(cal.sheet_csv(rows)))
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list[str](cal.CSV_COLUMNS), lineterminator="\n")
    w.writeheader()
    by_key = {r.key: r for r in rows}
    for row in reader:
        row["label"] = label(by_key[row["key"]])
        w.writerow(row)
    return out.getvalue()


def test_csv_round_trip_and_validation():
    rows = sheet()
    labels = cal.labels_from_csv(fill(rows), rows)
    assert len(labels.labels) == 300
    assert all(e.label == "supported" for e in labels.labels.values())
    imported = cal.import_labels(rows, labels)
    assert {lab.split for lab in imported} == {"tuning", "heldout"}
    edited = fill(rows).replace(rows[0].claim_text, "Something else entirely.", 1)
    with pytest.raises(ValueError, match="edited"):
        cal.labels_from_csv(edited, rows)
    with pytest.raises(ValueError, match="unknown claim key"):
        cal.labels_from_csv(fill(rows).replace(rows[0].key, "kdeadbeef0000", 1), rows)
    with pytest.raises(ValueError, match="label must be"):
        cal.labels_from_csv(fill(rows, lambda r: "maybe"), rows)
    partial = labels.model_copy(update={"labels": dict(list(labels.labels.items())[:10])})
    with pytest.raises(ValueError, match="no first-pass label"):
        cal.import_labels(rows, partial)
    not_relabel = next(r for r in rows if not r.relabel)
    wrong = cal.LabelFile(
        sheet_id=rows[0].sheet_id,
        round="relabel",
        labels={not_relabel.key: labels.labels[not_relabel.key]},
    )
    with pytest.raises(ValueError, match="relabel set"):
        cal.import_labels(rows, labels, wrong)


def _get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
        return r.read().decode()


def _post(port, body):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/label",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


@pytest.mark.safety
def test_labelling_page_is_local_blind_and_autosaves(tmp_path):
    rows = sheet()
    path = tmp_path / "labels.json"
    server = serve_labeller(rows, path, seed=7)
    assert server.server_address[0] == "127.0.0.1"
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        page = _get(port, "/")
        item = json.loads(_get(port, "/api/item"))
        assert set(item) == {
            "done",
            "index",
            "total",
            "labelled",
            "key",
            "claim",
            "cited",
            "label",
            "extraction_error",
        }
        blob = (page + json.dumps(item)).casefold()
        assert not any(f":{m}" in blob for m in MODELS) and "claude-sonnet" not in blob
        _post(port, {"key": item["key"], "label": "unsupported", "extraction_error": True})
        saved = load_labels(path)
        assert saved is not None and saved.round == "first"
        assert saved.labels[item["key"]].label == "unsupported"
        assert saved.labels[item["key"]].extraction_error
        nxt = json.loads(_get(port, "/api/item"))
        assert nxt["key"] != item["key"] and nxt["labelled"] == 1
    finally:
        server.shutdown()
        server.server_close()


def test_relabel_round_hides_first_pass_labels_and_reorders(tmp_path):
    rows = sheet()
    first = tmp_path / "first.json"
    s1 = serve_labeller(rows, first, seed=7)
    for r in rows:
        s1.labeller.record(r.key, "supported", False)  # type: ignore[attr-defined]
    s1.server_close()
    s2 = serve_labeller(rows, tmp_path / "relabel.json", round_="relabel", seed=7)
    state = s2.labeller  # type: ignore[attr-defined]
    view = state.view()
    assert view["label"] is None and view["total"] == sum(r.relabel for r in rows)
    order = [r.answer for r in state.items]
    first_order = [r.answer for r in rows if r.relabel]
    assert order != first_order  # fresh order
    s2.server_close()


# ---- CAL-03/04 agreement ---------------------------------------------------------------------


def verdict(cid, v, h=JUDGE_PROMPT_HASH):
    return ClaimVerdict(
        claim_id=cid,
        judge="j",
        verdict=v,
        evidence=(1,) if v == "supported" else (),
        rationale="",
        judge_prompt_hash=h,
    )


def test_kappa_matches_hand_computation():
    assert cal.kappa_from(40, 5, 5, 50) == pytest.approx((0.9 - 0.505) / 0.495)
    assert cal.kappa_from(10, 0, 0, 0) == 1.0  # both constant and agreeing
    assert cal.kappa_from(0, 10, 0, 0) == 0.0


def labelled_world(agree_rate=0.9):
    rows = sheet()
    import random

    rng = random.Random(3)
    first = lf(
        rows,
        {
            r.key: cal.LabelEntry(label="supported" if rng.random() < 0.6 else "unsupported")
            for r in rows
        },
    )
    labels = cal.import_labels(rows, first)
    verdicts = {}
    for lab, r in zip(labels, rows, strict=True):
        same = rng.random() < agree_rate
        v = lab.label if same else ("unsupported" if lab.label == "supported" else "supported")
        verdicts[r.claim_id] = verdict(r.claim_id, v)
    return rows, labels, verdicts


def test_agreement_on_heldout_with_confusion_and_families():
    _rows, labels, verdicts = labelled_world()
    a = cal.agreement(
        labels,
        verdicts,
        judge="j",
        task="pubmedqa",
        family_of=FAMILY.__getitem__,
        seed=1,
        n_resamples=500,
    )
    held = [lab for lab in labels if lab.split == "heldout"]
    (tp, fn), (fp, tn) = a.confusion
    assert tp + fn + fp + tn == len(held)
    assert a.agreement == pytest.approx((tp + tn) / len(held))
    assert a.kappa.lo <= a.kappa.point <= a.kappa.hi and a.kappa.point == pytest.approx(
        cal.kappa_from(tp, fn, fp, tn)
    )
    assert set(a.by_family) == {"anthropic", "openai"}
    assert a.task == "pubmedqa" and a.judge_prompt_hash == JUDGE_PROMPT_HASH


def test_extraction_errors_and_unjudged_claims_are_left_out():
    _rows, labels, verdicts = labelled_world()
    held = [lab for lab in labels if lab.split == "heldout"]
    flagged = [
        lab.model_copy(update={"extraction_error": True}) if lab is held[0] else lab
        for lab in labels
    ]
    del verdicts[held[1].claim_id]
    a = cal.agreement(
        flagged,
        verdicts,
        judge="j",
        task="t",
        family_of=FAMILY.__getitem__,
        seed=1,
        n_resamples=200,
    )
    assert sum(sum(r) for r in a.confusion) == len(held) - 2
    assert cal.extraction_error_rate(flagged) == pytest.approx(1 / len(labels))


def test_cluster_bootstrap_is_wider_when_claims_within_an_answer_agree_together():
    import random

    rng = random.Random(0)
    clustered, independent = [], []
    for a in range(40):
        human = rng.random() < 0.5
        judge = human if rng.random() < 0.8 else not human
        for k in range(5):  # five claims per answer, all moving together
            clustered.append(cal._Pair(f"a{a}", human, judge))
            independent.append(cal._Pair(f"a{a}-{k}", human, judge))
    c = cal.cluster_kappa(clustered, seed=1, n_resamples=1000)
    i = cal.cluster_kappa(independent, seed=1, n_resamples=1000)
    assert c.point == pytest.approx(i.point)
    assert (c.hi - c.lo) > 1.5 * (i.hi - i.lo)


def test_verdicts_under_another_rubric_cannot_be_certified():
    _rows, labels, verdicts = labelled_world()
    k = next(lab.claim_id for lab in labels if lab.split == "heldout")
    verdicts[k] = verdict(k, "supported", h="0" * 16)
    with pytest.raises(ValueError, match="different rubric"):
        cal.agreement(labels, verdicts, judge="j", task="t", family_of=FAMILY.__getitem__, seed=1)


def test_self_consistency_compares_the_same_claims_only():
    rows, labels, _ = labelled_world()
    relabel_rows = [r for r in rows if r.relabel]
    first = {lab.claim_id: lab for lab in labels}
    again = lf(
        rows,
        {r.key: cal.LabelEntry(label=first[r.claim_id].label) for r in relabel_rows},
        "relabel",
    )
    full = cal.import_labels(
        rows, lf(rows, {r.key: cal.LabelEntry(label=first[r.claim_id].label) for r in rows}), again
    )
    sc = cal.self_consistency(full, seed=1, n_resamples=200)
    assert sc is not None and sc.point == pytest.approx(1.0)
    assert cal.self_consistency(labels, seed=1) is None


# ---- CAL-05 rubric-bound report --------------------------------------------------------------


def test_report_is_bound_to_task_and_rubric():
    _rows, labels, verdicts = labelled_world()
    report = cal.calibration_report(
        labels,
        {"j1": verdicts, "j2": verdicts},
        task="pubmedqa",
        family_of=FAMILY.__getitem__,
        seed=1,
        scored_at=T0,
        n_resamples=200,
    )
    assert report.judge_prompt_hash == JUDGE_PROMPT_HASH and report.n_answers == 50
    usable = cal.usable_agreements(report, "pubmedqa")
    assert calibration_status_of(("j1", "j2"), usable, task="pubmedqa") == "calibrated"
    stale = report.model_copy(update={"judge_prompt_hash": "0" * 16, "agreements": ()})
    assert cal.usable_agreements(stale, "pubmedqa") == ()
    assert cal.usable_agreements(report, "medqa") == ()


# ---- CAL-06 regression gate ------------------------------------------------------------------


def test_kappa_drop_above_tolerance_fails():
    assert cal.kappa_regressions({"j": 0.80}, {"j": 0.76}) == []
    assert cal.kappa_regressions({"j": 0.80}, {"j": 0.74}) == [
        "j: held-out kappa fell 0.800 -> 0.740"
    ]
    assert cal.kappa_regressions({"j": 0.8}, {}) == ["j: no held-out kappa in the current run"]


@pytest.mark.judge
def _fixture():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    return json.loads((root / "fixtures" / "calibration" / "regression.json").read_text())


def _fixture_kappas(fx):
    labels = [CalibrationLabel.model_validate(x) for x in fx["labels"]]
    current = {}
    for judge, vs in fx["verdicts"].items():
        verdicts = {cid: verdict(cid, v) for cid, v in vs.items()}
        a = cal.agreement(
            labels,
            verdicts,
            judge=judge,
            task=fx["task"],
            family_of=lambda m: "other",
            seed=1,
            n_resamples=100,
        )
        current[judge] = a.kappa.point
    return current


@pytest.mark.judge
def test_regression_fixture_holds_its_baseline():
    fx = _fixture()
    assert cal.regression_problems(fx, _fixture_kappas(fx)) == []


def test_regression_fixture_fails_when_the_rubric_changes():
    fx = {**_fixture(), "judge_prompt_hash": "0" * 16}
    problems = cal.regression_problems(fx, _fixture_kappas(_fixture()))
    assert problems and "re-record" in problems[0]


# ---- CAL-07 publish guard --------------------------------------------------------------------


def test_publish_guard_needs_dual_calibrated_complete():
    from pathlib import Path

    base = RunSummary.model_validate_json(
        (
            Path(__file__).resolve().parents[1] / "fixtures" / "results_v1" / "run_summary.json"
        ).read_text()
    )
    assert base.judge_mode == "dual" and base.calibration_status == "calibrated"
    cal.publish_guard(base)
    unc = base.model_copy(update={"calibration_status": "uncalibrated", "judges": ()})
    with pytest.raises(cal.PublishRefused, match="calibration"):
        cal.publish_guard(unc)
    single = base.model_copy(update={"judge_mode": "single"})
    with pytest.raises(cal.PublishRefused, match="single-judge"):
        cal.publish_guard(single)


# ---- CAL-08 guide ----------------------------------------------------------------------------


def test_calibration_guide_exists_and_is_linked_from_the_stamp():
    from pathlib import Path

    from evalkit.pipeline import aggregate

    guide = Path(__file__).resolve().parents[2] / "docs" / "calibrate-your-task.md"
    text = guide.read_text(encoding="utf-8")
    for step in ("calibrate export", "calibrate label", "--relabel", "calibrate score", "held-out"):
        assert step in text
    assert "docs/calibrate-your-task.md" in aggregate.stamp_lines.__code__.co_consts.__repr__()
    assert not any(n in text.casefold() for n in ("claude-sonnet-5-5",))
    _ = BLIND_DENYLIST, Interval


# ---- A2.6 verifier regressions (L-26) ---------------------------------------------------------


def test_sheet_order_does_not_reveal_the_model():
    rows = sheet()
    answers = []
    for r in rows:
        if not answers or answers[-1] != r.answer:
            answers.append(r.answer)
    models = [next(x.claim_id for x in rows if x.answer == a).split(":")[1] for a in answers]
    runs = 1 + sum(1 for a, b in itertools.pairwise(models) if a != b)
    assert runs > 25  # 100 answers from two models, shuffled: many alternations
    # claims of one answer stay together
    assert len(answers) == len({r.answer for r in rows})


def test_labels_from_another_run_never_attach(tmp_path):
    cases, records = world()
    sample = cal.export_sample(records, cases, seed=7)
    rows_a = cal.build_sheet(sample, records, cases, seed=7, secret="s")
    changed = [
        r.model_copy(
            update={
                "claims": tuple(
                    c.model_copy(update={"text": c.text + " Edited."}) for c in r.claims
                )
            }
        )
        for r in records
    ]
    rows_b = cal.build_sheet(sample, changed, cases, seed=7, secret="s")
    assert {r.key for r in rows_a}.isdisjoint({r.key for r in rows_b})  # same ids, new text
    labels_a = lf(rows_a, {r.key: cal.LabelEntry(label="supported") for r in rows_a})
    with pytest.raises(ValueError, match="unknown claim key"):
        cal.import_labels(rows_b, labels_a.model_copy(update={"sheet_id": rows_b[0].sheet_id}))
    other_sheet = cal.build_sheet(sample, records, cases, seed=7)  # fresh secret
    with pytest.raises(ValueError, match="another sheet"):
        cal.import_labels(other_sheet, labels_a)


def test_keys_cannot_be_reversed_by_guessing_claim_ids():
    rows = sheet()
    import hashlib

    r = rows[0]
    assert r.key != "k" + hashlib.sha256(r.claim_id.encode()).hexdigest()[:12]


def test_relabel_round_refuses_the_first_pass_file(tmp_path):
    rows = sheet()
    first = tmp_path / "first.json"
    s1 = serve_labeller(rows, first, seed=7)
    s1.labeller.record(rows[0].key, "supported", False)  # type: ignore[attr-defined]
    s1.server_close()
    with pytest.raises(ValueError, match="separate file"):
        serve_labeller(rows, first, round_="relabel", seed=7)


def test_a_judge_must_cover_the_held_out_claims():
    _rows, labels, verdicts = labelled_world()
    held = [lab.claim_id for lab in labels if lab.split == "heldout"]
    sparse = {k: v for k, v in verdicts.items() if k in held[:3]}
    with pytest.raises(cal.InsufficientCoverage, match="at least 95%"):
        cal.agreement(labels, sparse, judge="j", task="t", family_of=FAMILY.__getitem__, seed=1)
    a = cal.agreement(
        labels, verdicts, judge="j", task="t", family_of=FAMILY.__getitem__, seed=1, n_resamples=100
    )
    assert a.coverage == 1.0 and a.n_claims == len(held)


def test_kappa_on_an_asymmetric_table_and_confusion_orientation():
    # tp=30 fn=10 fp=5 tn=55: po=0.85, pe=(40*35+60*65)/100^2=0.53
    assert cal.kappa_from(30, 10, 5, 55) == pytest.approx((0.85 - 0.53) / 0.47)
    pairs = [cal._Pair("a", True, True)] * 3 + [cal._Pair("b", True, False)] * 2
    pairs += [cal._Pair("c", False, True)] + [cal._Pair("d", False, False)] * 4
    assert cal._confusion(pairs) == (3, 2, 1, 4)


def test_per_family_kappa_is_computed_on_that_family_only():
    _rows, labels, verdicts = labelled_world()
    # make the openai family's verdicts the opposite of the owner's
    flipped = dict(verdicts)
    by_id = {lab.claim_id: lab for lab in labels}
    for cid in verdicts:
        if ":gpt-6.1-sol:" in cid:
            human = by_id[cid].label
            flipped[cid] = verdict(cid, "unsupported" if human == "supported" else "supported")
        else:
            flipped[cid] = verdict(cid, by_id[cid].label)
    a = cal.agreement(
        labels, flipped, judge="j", task="t", family_of=FAMILY.__getitem__, seed=1, n_resamples=200
    )
    assert a.by_family["anthropic"].point == pytest.approx(1.0)
    assert a.by_family["openai"].point < 0


def test_kappa_interval_is_the_95_percent_cluster_percentile():
    from evalkit.stats import Resamples, _quantile

    _rows, labels, verdicts = labelled_world()
    a = cal.agreement(
        labels, verdicts, judge="j", task="t", family_of=FAMILY.__getitem__, seed=4, n_resamples=500
    )
    held = [lab for lab in labels if lab.split == "heldout"]
    answers = sorted({cal._answer_of(lab.claim_id) for lab in held})
    per = {x: [0, 0, 0, 0] for x in answers}
    for lab in held:
        h, j = lab.label == "supported", verdicts[lab.claim_id].verdict == "supported"
        per[cal._answer_of(lab.claim_id)][(0 if j else 1) if h else (2 if j else 3)] += 1
    rs = Resamples(len(answers), 500, seed=4)
    boot = sorted(
        cal.kappa_from(*[sum(per[answers[i]][k] for i in row) for k in range(4)]) for row in rs.rows
    )
    assert a.kappa.lo == pytest.approx(min(_quantile(boot, 0.025), a.kappa.point))
    assert a.kappa.hi == pytest.approx(max(_quantile(boot, 0.975), a.kappa.point))


def test_tuning_agreement_is_marked_and_never_certifies():
    from evalkit.pipeline.aggregate import calibration_status_of

    _rows, labels, verdicts = labelled_world()
    t = cal.agreement(
        labels,
        verdicts,
        judge="j",
        task="pubmedqa",
        family_of=FAMILY.__getitem__,
        seed=1,
        split="tuning",
        n_resamples=100,
    )
    assert t.split == "tuning"
    assert calibration_status_of(("j",), [t], task="pubmedqa") == "uncalibrated"


def test_a_held_out_set_scored_under_another_rubric_is_refused():
    _rows, labels, verdicts = labelled_world()
    kw: dict[str, Any] = dict(
        task="pubmedqa", family_of=FAMILY.__getitem__, seed=1, scored_at=T0, n_resamples=100
    )
    report = cal.calibration_report(labels, {"j": verdicts}, **kw)
    old = report.model_copy(update={"judge_prompt_hash": "0" * 16, "agreements": ()})
    with pytest.raises(cal.HeldoutReused, match="fresh held-out"):
        cal.calibration_report(labels, {"j": verdicts}, previous=[old], **kw)
    cal.calibration_report(labels, {"j": verdicts}, previous=[report], **kw)  # same rubric: ok


def test_exact_tolerance_drop_passes_consistently():
    assert cal.kappa_regressions({"j": 0.80}, {"j": 0.75}) == []
    assert cal.kappa_regressions({"j": 0.70}, {"j": 0.65}) == []


def test_csv_with_bom_and_duplicate_keys():
    rows = sheet()
    assert len(cal.labels_from_csv("\ufeff" + fill(rows), rows).labels) == 300
    lines = fill(rows).splitlines()
    dup = "\n".join([*lines, lines[1]]) + "\n"
    with pytest.raises(ValueError, match="appears twice"):
        cal.labels_from_csv(dup, rows)


@pytest.mark.safety
def test_labeller_rejects_cross_site_and_malformed_posts(tmp_path):
    rows = sheet()
    server = serve_labeller(rows, tmp_path / "l.json", seed=7)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def raw(headers, body=b"{}"):
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/label", data=body, headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    try:
        key = rows[0].key
        good = json.dumps({"key": key, "label": "supported", "extraction_error": False}).encode()
        assert raw({"Content-Type": "text/plain"}, good) == 415  # simple cross-site request
        assert raw({"Content-Type": "application/json", "Host": "evil.example"}, good) == 403
        bad_bool = json.dumps({"key": key, "label": "supported", "extraction_error": "false"})
        assert raw({"Content-Type": "application/json"}, bad_bool.encode()) == 400
        unknown = json.dumps({"key": "k000", "label": "supported"}).encode()
        assert raw({"Content-Type": "application/json"}, unknown) == 400
        assert raw({"Content-Type": "application/json"}, b"x" * 20_000) == 413
        assert raw({"Content-Type": "application/json"}, good) == 200
        assert [p.name for p in tmp_path.iterdir()] == ["l.json"]  # atomic: no temp files
    finally:
        server.shutdown()
        server.server_close()


def test_publish_guard_refuses_pending_judging():
    from pathlib import Path

    base = RunSummary.model_validate_json(
        (
            Path(__file__).resolve().parents[1] / "fixtures" / "results_v1" / "run_summary.json"
        ).read_text()
    )
    pending = base.model_copy(update={"judging_status": "pending", "finished_at": None})
    with pytest.raises(cal.PublishRefused, match="finished judging"):
        cal.publish_guard(pending)
