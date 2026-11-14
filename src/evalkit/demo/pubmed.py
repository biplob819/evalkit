"""PubMed abstract fetch (D3.1, CMP-02) through NCBI E-utilities. Nothing is stored."""

from __future__ import annotations

import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable

from evalkit.text.sentences import split_sentences

EFETCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
TOOL = "evalkit"  # NCBI asks callers to name their tool; no personal contact details are sent
PMID_RE = re.compile(r"^\d{1,9}$")
MAX_BYTES = 2_000_000  # one abstract is a few kilobytes

Fetcher = Callable[[str, float], bytes]


class PubMedError(ValueError):
    pass


def _http_get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "evalkit-demo"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # fixed https URL
        body = resp.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise PubMedError("PubMed response is too large")
    return body


def parse_abstract(xml_bytes: bytes) -> str:
    """The abstract text (labelled sections joined) from an efetch XML response."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        raise PubMedError(f"PubMed returned malformed XML: {exc}") from None
    parts = []
    for node in root.iter("AbstractText"):
        text = "".join(node.itertext()).strip()
        if text:
            parts.append(text)
    return " ".join(parts)


def fetch_abstract(pmid: str, *, timeout: float = 8.0, get: Fetcher = _http_get) -> list[str]:
    """The abstract of `pmid` split into sentences (cited 1..n). Held in memory only."""
    pmid = pmid.strip()
    if not PMID_RE.match(pmid):
        raise PubMedError(f"{pmid!r} is not a PubMed ID (digits only)")
    query = urllib.parse.urlencode(
        {"db": "pubmed", "id": pmid, "retmode": "xml", "rettype": "abstract", "tool": TOOL}
    )
    try:
        body = get(f"{EFETCH}?{query}", timeout)
    except OSError as exc:
        raise PubMedError(f"could not reach PubMed: {exc}") from None
    text = parse_abstract(body)
    if not text:
        raise PubMedError(f"PubMed {pmid} has no abstract")
    sentences = split_sentences(text)
    if not sentences:
        raise PubMedError(f"PubMed {pmid} abstract could not be split into sentences")
    return sentences
