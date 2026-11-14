"""SciFact loader (OSS-04): a synthetic release archive, so no network."""

from __future__ import annotations

import gzip
import io
import json
import tarfile

import pytest

from evalkit.datasets import scifact as S
from evalkit.run import load_cases
from evalkit.tasks import builtin_task

CLAIMS = [
    {"id": 1, "claim": "X lowers Y.", "evidence": {}, "cited_doc_ids": [10, 11]},
    {
        "id": 3,
        "claim": "Z raises W.",
        "evidence": {"11": [{"sentences": [0, 2], "label": "SUPPORT"}]},
        "cited_doc_ids": [10, 11],
    },
    {
        "id": 2,
        "claim": "Q prevents R.",
        "evidence": {"10": [{"sentences": [1], "label": "CONTRADICT"}]},
        "cited_doc_ids": [10],
    },
]
CORPUS = [
    {"doc_id": 10, "title": "Ten", "abstract": ["S one.", "S two.", "S three."]},
    {"doc_id": 11, "title": "Eleven", "abstract": ["E one.", "E two.", "E three.", "E four."]},
]


def tarball(claims=CLAIMS, corpus=CORPUS, *, drop: str = "") -> bytes:
    buf = io.BytesIO()
    with tarfile.TarFile(fileobj=buf, mode="w") as tar:
        files = {
            "data/claims_dev.jsonl": "\n".join(json.dumps(c) for c in claims),
            "data/corpus.jsonl": "\n".join(json.dumps(d) for d in corpus),
        }
        for name, text in files.items():
            if name == drop:
                continue
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return gzip.compress(buf.getvalue())


def test_claims_become_cases_with_the_evidence_abstract_and_mapped_labels():
    rows = S.slim_from_tarball(tarball())
    cases = S.parse_scifact(rows)
    assert [c.id for c in cases] == ["scifact-1", "scifact-2", "scifact-3"]
    by = {c.id: c for c in cases}
    assert by["scifact-1"].reference_label == "not enough info"
    assert by["scifact-1"].context == ("S one.", "S two.", "S three.")  # first cited doc
    assert by["scifact-3"].reference_label == "supports"
    assert by["scifact-3"].context[0] == "E one."  # the doc that carries the evidence
    assert by["scifact-2"].reference_label == "refutes"
    assert {r["id"]: r["rationale"] for r in rows}[3] == [1, 3]  # 1-based, as cited
    assert all(c.question and c.source == S.SOURCE for c in cases)


def test_labels_match_the_builtin_task():
    task = builtin_task("scifact")
    assert set(task.labels) == {*S.LABELS.values(), S.NEI} == set(S.EXPECTED_LABELS)


def test_unpinned_download_is_refused_and_never_cached(tmp_path):
    path = tmp_path / "scifact.json"
    with pytest.raises(S.DatasetError, match="checksum"):
        S.load_scifact(path=path, tarball=tarball())
    assert not path.exists()


def test_unverified_load_caches_and_reloads(tmp_path):
    path = tmp_path / "scifact.json"
    first = S.load_scifact(path=path, tarball=tarball(), verify=False)
    assert path.is_file() and len(first) == 3
    again = S.load_scifact(path=path, tarball=b"not used", verify=False)
    assert again == first


def test_a_damaged_cache_is_refused(tmp_path):
    path = tmp_path / "scifact.json"
    path.write_bytes(b'[{"id": 1}]')
    with pytest.raises(S.DatasetError, match="checksum"):
        S.load_scifact(path=path)


def test_an_archive_missing_a_file_is_a_clear_error(tmp_path):
    with pytest.raises(S.DatasetError, match="claims_dev"):
        S.slim_from_tarball(tarball(drop="data/claims_dev.jsonl"))
    with pytest.raises(S.DatasetError, match="expected format"):
        S.load_scifact(path=tmp_path / "x.json", tarball=b"not a tarball", verify=False)


def test_conflicting_labels_on_one_abstract_are_refused():
    claims = [
        {
            "id": 9,
            "claim": "C.",
            "evidence": {
                "10": [
                    {"sentences": [0], "label": "SUPPORT"},
                    {"sentences": [1], "label": "CONTRADICT"},
                ]
            },
            "cited_doc_ids": [10],
        }
    ]
    with pytest.raises(S.DatasetError, match="conflicting"):
        S.slim_from_tarball(tarball(claims=claims))


def test_the_run_loads_scifact_for_the_builtin_task(monkeypatch):
    seen = []
    monkeypatch.setattr("evalkit.run.load_scifact", lambda: seen.append(1) or [])
    assert load_cases(builtin_task("scifact"), None) == [] and seen == [1]
