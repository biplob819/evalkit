"""PubMedQA, expert-labelled subset PQA-L (DAT-01).

Downloaded once from the Hugging Face datasets server, reduced to the fields EvalKit uses,
cached under the user cache directory and verified against a pinned checksum on every
load. `long_answer` is dropped at load time: it is the paper's conclusion and would leak
the answer into the prompt.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from evalkit.schemas import Case
from evalkit.text import number_sentences

DATASET = "qiaojin/PubMedQA"
CONFIG = "pqa_labeled"
SOURCE = "pubmedqa/pqa_l"
EXPECTED_ROWS = 1000
EXPECTED_LABELS = {"yes": 552, "no": 338, "maybe": 110}
# sha256 of the canonical slim JSON (sorted by pubid); pinned 2026-11-14.
EXPECTED_SHA256 = "2f5f7fa1355bf5e577e29eb150dd9802687c193a24e48cbc21387e7d0a244911"
ROWS_URL = (
    "https://datasets-server.huggingface.co/rows?dataset=qiaojin/PubMedQA"
    "&config=pqa_labeled&split=train&offset={offset}&length=100"
)


class DatasetError(RuntimeError):
    pass


def cache_dir() -> Path:
    root = os.environ.get("EVALKIT_CACHE") or os.environ.get("XDG_CACHE_HOME")
    return Path(root) / "evalkit" if root else Path.home() / ".cache" / "evalkit"


def _slim(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "pubid": row["pubid"],
        "question": row["question"],
        "contexts": row["context"]["contexts"],
        "final_decision": row["final_decision"],
    }


def _canonical(rows: list[dict[str, Any]]) -> bytes:
    rows = sorted(rows, key=lambda r: r["pubid"])
    return json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _download() -> list[dict[str, Any]]:  # pragma: no cover - network
    rows: list[dict[str, Any]] = []
    for offset in range(0, EXPECTED_ROWS, 100):
        with urllib.request.urlopen(ROWS_URL.format(offset=offset), timeout=60) as resp:
            rows += [_slim(r["row"]) for r in json.load(resp)["rows"]]
    return rows


def parse_pqa_l(rows: list[dict[str, Any]]) -> list[Case]:
    """Slim PQA-L rows → Cases with 1-based numbered context sentences."""
    cases = []
    for r in sorted(rows, key=lambda r: r["pubid"]):
        cases.append(
            Case(
                id=f"pqa-{r['pubid']}",
                question=r["question"],
                context=number_sentences(r["contexts"]),
                reference_label=r["final_decision"],
                source=SOURCE,
            )
        )
    return cases


def _verify(blob: bytes, where: str) -> None:
    digest = hashlib.sha256(blob).hexdigest()
    if digest != EXPECTED_SHA256:
        raise DatasetError(
            f"{where}: checksum {digest[:12]}… does not match the pinned PQA-L "
            f"({EXPECTED_SHA256[:12]}…). The source data changed or the file is damaged."
        )


def _atomic_write(path: Path, blob: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def load_pqa_l(*, path: Path | None = None, verify: bool = True) -> list[Case]:
    """Load PQA-L from cache, downloading on first use.

    A download is verified before it is written, and written atomically, so a damaged or
    changed copy never lands in the cache. Network and parse failures raise DatasetError.
    """
    path = path or cache_dir() / "pubmedqa_pqa_l.json"
    if not path.is_file():
        try:
            rows = _download()
        except (urllib.error.URLError, TimeoutError, OSError, KeyError, ValueError) as exc:
            raise DatasetError(f"could not download PubMedQA PQA-L: {exc}") from exc
        blob = _canonical(rows)
        if verify:
            _verify(blob, "downloaded PQA-L")
        _atomic_write(path, blob)
    blob = path.read_bytes()
    if verify:
        _verify(blob, str(path))
    try:
        rows = json.loads(blob)
    except ValueError as exc:
        raise DatasetError(f"{path}: not valid JSON ({exc}); delete it to re-download") from exc
    cases = parse_pqa_l(rows)
    if verify and len(cases) != EXPECTED_ROWS:
        raise DatasetError(f"{path}: expected {EXPECTED_ROWS} rows, got {len(cases)}")
    return cases
