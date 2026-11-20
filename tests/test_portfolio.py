"""The root portfolio.yml `priorities` list behind the portfolio page's built, cut and later
toggle (request relayed by the portfolio session, 2026-11-20): its shape, the counts per call,
plain words without codes, and that every source names a real file or decision."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
COUNTS = {"built": (4, 7), "cut": (2, 5), "later": (2, 5)}
CODE = re.compile(r"\bD-\d+\b|\bUS\d+\b|\b[A-Z]\d+\.\d+\b|\br-[0-9a-f]{6,}\b|\b[A-Z]{3}-\d{2}\b")


def _priorities() -> list[dict[str, str]]:
    data = yaml.safe_load((ROOT / "portfolio.yml").read_text("utf-8"))
    return data["priorities"]


def test_every_item_has_the_four_fields_and_a_known_call():
    for p in _priorities():
        assert set(p) == {"item", "call", "why", "source"}, p
        assert p["call"] in COUNTS, p
        assert all(isinstance(v, str) and v.strip() for v in p.values()), p


def test_counts_per_call_are_in_range():
    items = _priorities()
    for call, (lo, hi) in COUNTS.items():
        n = sum(p["call"] == call for p in items)
        assert lo <= n <= hi, (call, n)


def test_items_are_short_and_why_is_one_plain_sentence_without_codes():
    for p in _priorities():
        assert len(p["item"].split()) <= 15, p["item"]
        assert p["why"].endswith("."), p["why"]
        assert len(re.findall(r"[.!?](?:\s|$)", p["why"])) == 1, p["why"]
        for field in ("item", "why"):
            assert not CODE.search(p[field]), (field, p[field])


def test_every_source_names_a_real_file_or_decision():
    decisions = (ROOT / "docs" / "decisions.md").read_text("utf-8")
    for p in _priorities():
        for part in p["source"].split(";"):
            ref = re.sub(r"\s*\(.*\)$", "", part.strip())
            if re.fullmatch(r"D-\d+", ref):
                assert re.search(rf"(?:\| (?:OWNER )?|## ){ref}\b", decisions), ref
            else:
                assert (ROOT / ref).is_file(), ref
