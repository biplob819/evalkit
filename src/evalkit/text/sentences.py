"""Deterministic, rule-based sentence splitting for scientific abstracts (DAT-02).

No model and no network: the same text always gives the same sentences, so a citation
number means the same sentence in the prompt, the scorer and the dashboard.
"""

from __future__ import annotations

import re

# Abbreviations that end with a period but do not end a sentence.
_ABBREVIATIONS = {
    "e.g",
    "i.e",
    "et al",
    "etc",
    "vs",
    "cf",
    "approx",
    "fig",
    "figs",
    "no",
    "nos",
    "vol",
    "dr",
    "mr",
    "mrs",
    "ms",
    "prof",
    "st",
    "jr",
    "sr",
    "inc",
    "ltd",
    "co",
    "dept",
    "univ",
    "ca",
    "resp",
    "ref",
    "refs",
    "eq",
    "eqs",
    "sec",
    "ch",
    "al",
    "s.d",
    "u.s",
    "u.k",
    "c.i",
    "jan",
    "feb",
    "mar",
    "apr",
    "jun",
    "jul",
    "aug",
    "sep",
    "sept",
    "oct",
    "nov",
    "dec",
}
# A candidate boundary: sentence punctuation, optional closing quote/bracket, whitespace.
_BOUNDARY = re.compile(r"""([.!?])(["')\]]*)\s+""")


def _ends_with_abbreviation(text: str) -> bool:
    tail = re.search(r"([A-Za-z][A-Za-z.]*)$", text)
    if not tail:
        return False
    word = tail.group(1).lower().rstrip(".")
    if word in _ABBREVIATIONS:
        return True
    if len(word) == 1 and word.isalpha():
        return _is_initial(text[: tail.start()])
    return False


def _is_initial(before: str) -> bool:
    """A lone letter is a name initial ("Dr. J. Smith", "John F. Kennedy") only when the
    word before it is capitalised or is itself an initial; "vitamin D." and "arm A." end
    sentences."""
    prev = before.rstrip().split(" ")[-1] if before.strip() else ""
    if not prev:
        return True
    return prev[0].isupper()


def _open_brackets(text: str) -> int:
    return text.count("(") - text.count(")") + text.count("[") - text.count("]")


_SPACED_DECIMAL_TAIL = re.compile(r"^\d+(?:\.\d+)?%")


def split_sentences(text: str) -> list[str]:
    """Split one paragraph into sentences.

    A boundary is `.`, `!` or `?` followed by whitespace and then an uppercase letter,
    digit or opening bracket, unless the text before it ends in a known abbreviation or a
    name initial ("J. Smith"), sits inside a bracket that closes later, or is a spaced
    decimal before a percentage ("14. 1%"). Decimals ("0.05") and "p < 0.05" never split
    because the period is not followed by whitespace.
    """
    text = " ".join(text.split())
    if not text:
        return []
    sentences: list[str] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        end = m.end()
        nxt = text[end : end + 1]
        if not nxt or not (nxt.isupper() or nxt.isdigit() or nxt in "([\"'"):
            continue
        candidate = text[start : m.start(1)]
        if m.group(1) == "." and _ends_with_abbreviation(candidate):
            continue
        if _open_brackets(text[start : m.end(2)]) > 0 and _open_brackets(text[start:]) <= 0:
            # Inside a bracket that closes later: "(P<0. 001)", "(95% C.I. 5.3-18.9)".
            # An unclosed bracket (a typo in the source) never suppresses later splits.
            continue
        if candidate[-1:].isdigit() and _SPACED_DECIMAL_TAIL.match(text[end:]):
            continue  # "a sampling of 14. 1% day surgery" is a spaced decimal, not an end
        sentences.append(text[start : m.end(2)].strip())
        start = end
    last = text[start:].strip()
    if last:
        sentences.append(last)
    return sentences


def number_sentences(paragraphs: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Split every paragraph and return one flat, ordered tuple; sentence i is cited as i+1."""
    out: list[str] = []
    for p in paragraphs:
        out.extend(split_sentences(p))
    return tuple(out)
