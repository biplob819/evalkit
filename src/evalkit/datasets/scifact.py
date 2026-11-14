"""SciFact dev claims (OSS-04): scientific claims checked against one abstract.

SciFact (Wadden et al., 2020): claims and evidence annotations CC BY 4.0, abstracts from
S2ORC under ODC-By 1.0 (see DATA_CARD.md). The release tarball is downloaded once, checked
against a pinned SHA-256 before anything is read from it, and only the two files EvalKit
needs are read, in memory (nothing from the archive is written to disk). The reduced cases
are cached as canonical JSON and verified against a second pinned checksum on every load.

One case per dev claim: the claim is the question, and the context is one cited abstract
(the one carrying evidence, else the first cited), already split into sentences by the
dataset, so the gold rationale sentence numbers line up with what the model cites.
"""

from __future__ import annotations

import gzip
import hashlib
import http.client
import io
import json
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from evalkit.datasets.pubmedqa import _atomic_write, cache_dir
from evalkit.schemas import Case

DATASET = "allenai/scifact"
SOURCE = "scifact/dev"
URL = "https://scifact.s3-us-west-2.amazonaws.com/release/latest/data.tar.gz"
TARBALL_SHA256 = "11c621288d41ac144d29b13b0f8503b3820b7d6e8b1f6ff24dff335c196d76be"
CASES_SHA256 = "b0aa46bf30129774889988a8dcd6e94f1f4b230ae7ba1f22fb1725e9465703df"
EXPECTED_ROWS = 300
EXPECTED_LABELS = {"supports": 124, "refutes": 64, "not enough info": 112}
LABELS = {"SUPPORT": "supports", "CONTRADICT": "refutes"}
NEI = "not enough info"
MAX_TARBALL_BYTES = 50_000_000  # the release is ~3 MB; refuse anything absurd


class DatasetError(RuntimeError):
    pass


def _download() -> bytes:  # pragma: no cover - network
    with urllib.request.urlopen(URL, timeout=120) as resp:
        blob = resp.read(MAX_TARBALL_BYTES + 1)
    if len(blob) > MAX_TARBALL_BYTES:
        raise DatasetError("SciFact download is larger than expected")
    return blob


def _member(tar: tarfile.TarFile, name: str) -> str:
    try:
        member = tar.getmember(name)
    except KeyError:
        raise DatasetError(f"SciFact archive has no {name}") from None
    if not member.isfile():
        raise DatasetError(f"SciFact archive member {name} is not a file")
    fh = tar.extractfile(member)
    if fh is None:
        raise DatasetError(f"SciFact archive member {name} is unreadable")
    return fh.read().decode("utf-8")


def _jsonl(text: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def slim_from_tarball(blob: bytes) -> list[dict[str, Any]]:
    """The dev claims, each with the one abstract it is checked against."""
    with tarfile.TarFile(fileobj=io.BytesIO(gzip.decompress(blob))) as tar:
        claims = _jsonl(_member(tar, "data/claims_dev.jsonl"))
        corpus = {d["doc_id"]: d for d in _jsonl(_member(tar, "data/corpus.jsonl"))}
    rows = []
    for c in claims:
        evidence = c.get("evidence") or {}
        doc_ids = [int(k) for k in evidence] or [int(x) for x in c["cited_doc_ids"]]
        doc_id = doc_ids[0]
        labels = {e["label"] for e in evidence.get(str(doc_id), [])}
        if len(labels) > 1:
            raise DatasetError(f"claim {c['id']} has conflicting labels {sorted(labels)}")
        label = LABELS[labels.pop()] if labels else NEI
        sentences = sorted({n for e in evidence.get(str(doc_id), []) for n in e["sentences"]})
        doc = corpus[doc_id]
        rows.append(
            {
                "id": c["id"],
                "claim": c["claim"],
                "doc_id": doc_id,
                "title": doc["title"],
                "abstract": doc["abstract"],
                "label": label,
                "rationale": [n + 1 for n in sentences],  # 1-based, as cited
            }
        )
    return rows


def canonical(rows: list[dict[str, Any]]) -> bytes:
    rows = sorted(rows, key=lambda r: r["id"])
    return json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def parse_scifact(rows: list[dict[str, Any]]) -> list[Case]:
    return [
        Case(
            id=f"scifact-{r['id']}",
            question=r["claim"],
            context=tuple(s.strip() for s in r["abstract"] if s.strip()),
            reference_label=r["label"],
            source=SOURCE,
        )
        for r in sorted(rows, key=lambda r: r["id"])
    ]


def _check(blob: bytes, expected: str, what: str) -> None:
    digest = hashlib.sha256(blob).hexdigest()
    if digest != expected:
        raise DatasetError(
            f"{what}: checksum {digest[:12]}… does not match the pinned SciFact release "
            f"({expected[:12]}…). The source changed or the file is damaged."
        )


def load_scifact(
    *, path: Path | None = None, tarball: bytes | None = None, verify: bool = True
) -> list[Case]:
    """Load SciFact dev from cache, downloading the release on first use. Failures raise
    DatasetError; a damaged or changed download never reaches the cache."""
    path = path or cache_dir() / "scifact_dev.json"
    if not path.is_file():
        try:
            blob = tarball if tarball is not None else _download()
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            raise DatasetError(f"could not download SciFact: {exc}") from exc
        if verify:
            _check(blob, TARBALL_SHA256, "downloaded SciFact release")
        try:
            rows = slim_from_tarball(blob)
        except (tarfile.TarError, OSError, EOFError, KeyError, ValueError, TypeError) as exc:
            raise DatasetError(f"SciFact release is not in the expected format: {exc}") from exc
        out = canonical(rows)
        if verify:
            _check(out, CASES_SHA256, "SciFact cases")
        _atomic_write(path, out)
    blob = path.read_bytes()
    if verify:
        _check(blob, CASES_SHA256, str(path))
    try:
        rows = json.loads(blob)
    except ValueError as exc:
        raise DatasetError(f"{path}: not valid JSON ({exc}); delete it to re-download") from exc
    cases = parse_scifact(rows)
    if verify and len(cases) != EXPECTED_ROWS:
        raise DatasetError(f"{path}: expected {EXPECTED_ROWS} claims, got {len(cases)}")
    return cases
