import hashlib
import json
from pathlib import Path

import pytest

from evalkit.datasets import pubmedqa
from evalkit.datasets.pubmedqa import DatasetError, load_pqa_l, parse_pqa_l
from evalkit.sampling import stratified_sample
from evalkit.schemas import Case
from evalkit.tasks import builtin_task, render_prompt
from evalkit.text import number_sentences, split_sentences

ROOT = Path(__file__).resolve().parents[2]
MINI = ROOT / "tests" / "fixtures" / "pubmedqa_mini.json"


def mini_rows():
    return json.loads(MINI.read_text(encoding="utf-8"))


# ---- DAT-01 loader --------------------------------------------------------------------


def test_parse_builds_cases_with_numbered_context_and_labels():
    cases = parse_pqa_l(mini_rows())
    assert len(cases) == 12
    c = cases[0]
    assert c.id.startswith("pqa-") and c.source == "pubmedqa/pqa_l"
    assert c.reference_label in {"yes", "no", "maybe"}
    assert len(c.context) >= len(mini_rows()[0]["contexts"])


def test_loader_uses_cache_and_verifies_checksum(tmp_path, monkeypatch):
    path = tmp_path / "pqa.json"
    blob = pubmedqa._canonical(mini_rows())
    path.write_bytes(blob)
    with pytest.raises(DatasetError, match="checksum"):
        load_pqa_l(path=path)
    monkeypatch.setattr(pubmedqa, "EXPECTED_SHA256", hashlib.sha256(blob).hexdigest())
    with pytest.raises(DatasetError, match="expected 1000 rows"):
        load_pqa_l(path=path)
    monkeypatch.setattr(pubmedqa, "EXPECTED_ROWS", 12)
    assert len(load_pqa_l(path=path)) == 12


def test_loader_downloads_once_then_reads_cache(tmp_path, monkeypatch):
    calls = []

    def fake_download():
        calls.append(1)
        return mini_rows()

    monkeypatch.setattr(pubmedqa, "_download", fake_download)
    path = tmp_path / "pqa.json"
    load_pqa_l(path=path, verify=False)
    load_pqa_l(path=path, verify=False)
    assert calls == [1]


@pytest.mark.safety
def test_long_answer_never_reaches_cases_or_prompts():
    raw = [
        {**r, "context": {"contexts": r["contexts"]}, "long_answer": "LEAKED CONCLUSION"}
        for r in mini_rows()
    ]
    slim = [pubmedqa._slim(r) for r in raw]
    assert all("long_answer" not in r for r in slim)
    task = builtin_task("pubmedqa")
    for case in parse_pqa_l(slim):
        assert "LEAKED CONCLUSION" not in render_prompt(task, case)
        assert "LEAKED CONCLUSION" not in case.model_dump_json()


# ---- DAT-02 sentence splitter --------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("First one. Second one.", ["First one.", "Second one."]),
        ("Smith et al. reported this. It held.", ["Smith et al. reported this.", "It held."]),
        (
            "Some drugs, e.g. aspirin, help. Others do not.",
            ["Some drugs, e.g. aspirin, help.", "Others do not."],
        ),
        ("That is, i.e. not this. Next.", ["That is, i.e. not this.", "Next."]),
        ("The rate was 0.05 lower. Then 3.2 rose.", ["The rate was 0.05 lower.", "Then 3.2 rose."]),
        ("Significant (p < 0.05). Confirmed.", ["Significant (p < 0.05).", "Confirmed."]),
        ("Dr. J. Smith led it. Done!", ["Dr. J. Smith led it.", "Done!"]),
        ("Why? Because. 12 patients died.", ["Why?", "Because.", "12 patients died."]),
        ("lowercase after period. stays together", ["lowercase after period. stays together"]),
        ("Adjusted for NIHSS (P<0. 001). For each", ["Adjusted for NIHSS (P<0. 001).", "For each"]),
        ("Risk was 10.0 (95%C.I. 5.3-18.9). Next.", ["Risk was 10.0 (95%C.I. 5.3-18.9).", "Next."]),
        (
            "Between Jan. 1, 1999, and Dec. 30, 2009. Then",
            ["Between Jan. 1, 1999, and Dec. 30, 2009.", "Then"],
        ),
        ("Levels of vitamin D. All patients", ["Levels of vitamin D.", "All patients"]),
        ("Infected with hepatitis C. The rate", ["Infected with hepatitis C.", "The rate"]),
        ("Assigned to arm A. In arm B", ["Assigned to arm A.", "In arm B"]),
        (
            "Measured at 60 min. Repeated at 48 h. The end",
            ["Measured at 60 min.", "Repeated at 48 h.", "The end"],
        ),
        ("John F. Kennedy spoke. Then", ["John F. Kennedy spoke.", "Then"]),
        (
            "An unclosed (bracket here. Next one. Last one.",
            ["An unclosed (bracket here.", "Next one.", "Last one."],
        ),
        (
            "A sampling of 14. 1% day surgery cases. Next",
            ["A sampling of 14. 1% day surgery cases.", "Next"],
        ),
        (
            "We enrolled 45 patients in 2010. 12 died.",
            ["We enrolled 45 patients in 2010.", "12 died."],
        ),
        ("", []),
    ],
)
def test_split_sentences(text, expected):
    assert split_sentences(text) == expected


def test_numbering_is_flat_ordered_and_stable():
    paras = ["A one. A two.", "B one."]
    assert number_sentences(paras) == ("A one.", "A two.", "B one.")
    rows = mini_rows()
    first = [c.context for c in parse_pqa_l(rows)]
    assert [c.context for c in parse_pqa_l(rows)] == first


# ---- DAT-03 sampler ------------------------------------------------------------------


def _cases(counts: dict[str, int]) -> list[Case]:
    out = []
    for label, n in counts.items():
        out += [
            Case(
                id=f"{label}-{i:04d}",
                question="q",
                context=("s",),
                reference_label=label,
                source="t",
            )
            for i in range(n)
        ]
    return out


def test_same_seed_same_sample_and_order():
    cases = _cases({"yes": 552, "no": 338, "maybe": 110})
    a = stratified_sample(cases, 200, seed=7)
    b = stratified_sample(list(reversed(cases)), 200, seed=7)
    assert [c.id for c in a.cases] == [c.id for c in b.cases]
    assert [c.id for c in stratified_sample(cases, 200, seed=8).cases] != [c.id for c in a.cases]


@pytest.mark.parametrize("n", [1, 7, 50, 200, 333, 1000])
def test_label_mix_within_one_case_of_population_share(n):
    pop = {"yes": 552, "no": 338, "maybe": 110}
    s = stratified_sample(_cases(pop), n, seed=7)
    assert sum(s.label_mix.values()) == n
    for label, count in pop.items():
        assert abs(s.label_mix.get(label, 0) - n * count / 1000) <= 1


def test_sample_size_is_validated():
    with pytest.raises(ValueError):
        stratified_sample(_cases({"yes": 3}), 4, seed=1)


# ---- DAT-04 task file ----------------------------------------------------------------


@pytest.mark.contract
def test_pubmedqa_task_loads_and_renders_numbered_sentences():
    task = builtin_task("pubmedqa")
    assert task.labels == ("yes", "no", "maybe")
    case = parse_pqa_l(mini_rows())[0]
    prompt = render_prompt(task, case)
    assert "[1] " in prompt and f"[{len(case.context)}] " in prompt
    assert case.question in prompt and "yes / no / maybe" in prompt
    assert "{" not in prompt.split("Reply with JSON only")[0]


def test_unknown_builtin_task_lists_available():
    with pytest.raises(ValueError, match="pubmedqa"):
        builtin_task("nope")


# ---- DAT-05 / DAT-06 data card -------------------------------------------------------


@pytest.mark.contract
def test_data_card_has_required_sections():
    text = (ROOT / "DATA_CARD.md").read_text(encoding="utf-8")
    for heading in (
        "## Source",
        "## Licence",
        "## Subset and size",
        "## Sampling",
        '## "Maybe" policy',
        "## Contamination caveat",
        "## Contamination probe",
        "## What this data cannot tell you",
    ):
        assert heading in text, heading
    assert "MIT" in text and "1,000" in text and "long_answer" in text


def test_paraphrase_probe_spec_is_complete():
    text = (ROOT / "DATA_CARD.md").read_text(encoding="utf-8")
    probe = text.split("## Contamination probe", 1)[1].split("\n## ", 1)[0]
    for item in ("**Questions:**", "**Paraphrases:**", "**Check:**", "**Storage:**", "**Report:**"):
        assert item in probe, item
    for pinned in (
        "exactly 100",
        "seed 7",
        "55 yes, 34 no, 11 maybe",
        "replaces it",
        "gpt-6.1-terra",
        "Claude Opus 5.5",
        "spot-checks 20",
        "src/evalkit/tasks/pubmedqa_paraphrase.jsonl",
        "reference_label",
        "check_verdict",
        "10,000 resamples",
    ):
        assert pinned in probe, pinned


@pytest.mark.contract
def test_data_card_claims_match_the_code():
    card = (ROOT / "DATA_CARD.md").read_text(encoding="utf-8")
    assert "552 yes, 338 no, 110 maybe" in card
    assert pubmedqa.EXPECTED_LABELS == {"yes": 552, "no": 338, "maybe": 110}
    assert "PubMedQA" in (ROOT / "README.md").read_text(encoding="utf-8")
    from evalkit.schemas import RunSummary

    # The card promises the label mix in run summaries only from A2.5 on; keep it honest.
    if "label_mix" not in RunSummary.model_fields:
        assert "from node A2.5" in card


# ---- golden pins (cross-process reproducibility) ---------------------------------------


def test_mini_fixture_golden_values():
    cases = parse_pqa_l(mini_rows())
    digest = hashlib.sha256(json.dumps([c.context for c in cases]).encode()).hexdigest()
    assert digest == "07c1d95b29cdaa30e79c5f897433d4f89332a5a574db2934b03d0e1b9270547c"
    assert sum(len(c.context) for c in cases) == 109
    assert [c.id for c in stratified_sample(cases, 6, seed=7).cases] == [
        "pqa-10223070",
        "pqa-8017535",
        "pqa-7547656",
        "pqa-7664228",
        "pqa-1571683",
        "pqa-2224269",
    ]


def test_duplicate_ids_are_rejected_and_zero_labels_reported():
    with pytest.raises(ValueError, match="unique"):
        stratified_sample(_cases({"yes": 2}) * 2, 2, seed=1)
    s = stratified_sample(_cases({"yes": 99, "maybe": 1}), 1, seed=1)
    assert s.label_mix == {"maybe": 0, "yes": 1}


def test_cache_dir_honours_evalkit_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("EVALKIT_CACHE", str(tmp_path))
    assert pubmedqa.cache_dir() == tmp_path / "evalkit"


def test_failed_download_is_a_dataset_error_and_writes_nothing(tmp_path, monkeypatch):
    import urllib.error

    def offline():
        raise urllib.error.URLError("no network")

    monkeypatch.setattr(pubmedqa, "_download", offline)
    with pytest.raises(DatasetError, match="could not download"):
        load_pqa_l(path=tmp_path / "pqa.json")
    assert not (tmp_path / "pqa.json").exists()


def test_changed_upstream_data_is_never_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(pubmedqa, "_download", mini_rows)
    with pytest.raises(DatasetError, match="downloaded PQA-L"):
        load_pqa_l(path=tmp_path / "pqa.json")
    assert list(tmp_path.iterdir()) == []


def test_damaged_cache_file_is_a_dataset_error(tmp_path):
    (tmp_path / "pqa.json").write_text("{half a file", encoding="utf-8")
    with pytest.raises(DatasetError, match="not valid JSON"):
        load_pqa_l(path=tmp_path / "pqa.json", verify=False)
