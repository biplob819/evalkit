"""Answer from rows (AFR-01): every number written in an agent's answer must be backed by the
result rows it was computed from.

Ported from agentic-analytics `src/agentic_analytics/scoring/numbers.py` (SCR-04, MIT licence,
Copyright (c) 2026 Biplob Chakraborty, the same author), with four changes for a general
library: the relative tolerance is a parameter, bare years are claims unless the caller gives a
`year_range`, a written minus must match a negative cell, and an `x` multiple may be derived as
the ratio of the cells of two rows the sentence names.

A number is supported when one of these holds:

1. **A result cell matches it** to the precision displayed, or within 0.5% relative.
   *Displayed precision* is half a unit of the last digit written, in the number's own scale:
   ``190k`` has a unit of 1,000 and covers 189,500 to 190,500; ``£254.3k`` covers 254,250 to
   254,350; ``1.2m`` covers 1,150,000 to 1,250,000; ``12.5%`` covers 12.45 to 12.55. The 0.5%
   rule covers trailing-zero roundings such as ``£190,000`` for 190,312. A number written
   with a minus (``-3.2%``) needs a negative cell; an unsigned one may match either sign
   ("fell 3.2%" is backed by a cell of -3.2).
   *Percentages* compare by the column's name. In a ``share``, ``ratio``, ``fraction``,
   ``proportion`` or ``rate`` column a cell of at most 1 is a fraction, so 12.5% matches 0.125
   there and "0.2%" does not match a share of 0.2. In a ``pct`` or ``percent`` column a cell of
   at most 1 may be either, so both readings are tried. Every other cell is in percent units.
2. **It appears in the question** (same value after scaling).
3. **It is a year in the data range** (only when `year_range` is given): a bare four-digit
   integer (no currency, separator, decimals or suffix) inside ``year_range``, or a decade
   that overlaps it ("late 1990s").
4. **It is written inside a text cell or a column name** of the result, read with the same
   rules as the answer ("FLAT 12", "10 DOWNING STREET", a column "SALES 5000"), same value.
5. **It is the N of a top-N question** or **the number of rows returned**. Both apply to
   plain integers only (no currency, sign, decimals, suffix or percent).
6. **It is an ``x`` multiple derivable as stated**: "Kensington is 6.6x Manchester" is backed
   when the sentence (up to 200 characters each side) names two rows, by a word of a text
   cell that belongs to one row only, and their cells in one numeric column have that ratio
   to the precision displayed (6.56 rounds to 6.6 or 7, not 2). The rows named before the
   multiple are the numerator and those after it the denominator (either order when all are on
   one side); both cells have one sign; when the sentence names a numeric column ("median
   price") only that column counts. With fewer than two named rows nothing is derived, as in
   the reference: a multiple then needs a cell that matches it. At most 20,000 cell pairs
   are tried.

What is not a number claim and is skipped: digits after letters (postcodes ``M1``, ``SW1A``,
quarters ``Q3``); ordinals (``3rd``); one- or two-digit numbers glued to a word (``2bed``,
``10am``); compounds of up to three digits (``10-year``); list numbering of up to two digits at
the start of a line (``1.``, ``2)``); a one- or two-digit rank in the first cell of a Markdown
table row with more cells; ``#1`` and ``[1]``; fenced code blocks; times (``10:30``); and the
parts of a date other than its year (``2024-01-01``, ``01/03/2024``, ``31/12/24``,
``31 December 2024``, ``January 31, 2024``, ``Q3 '24``; ``2024-25`` and ``2024-03`` count as
2024). Larger numbers are claims even when glued to a word (``999homes``, ``260,000-odd``,
``£260,000-plus``) or written ``999.`` at the start of a line.

Formats read: ``£ $ €`` and ``GBP USD EUR`` prefixes, ``c.`` (circa), thousands separators,
decimals, a leading minus (ASCII or Unicode), ``k K m M mn bn b B`` suffixes, ``thousand
million billion``, ``%``, ``percent``, ``per cent``, ``percentage points``, ``pp``, ``x``
multiples, number words that carry a scale word (``three million``, ``four thousand one
hundred``, ``half a million``, ``a quarter of a million``; at most 12 words read as one
number), and ranges, where ``2019-2024`` is
two years and ``£1.2-1.3m`` scales the first figure by the second's suffix (either reading may
match). Locale forms are parsed and checked like any number: Indian grouping
(``12,34,567`` = 1,234,567) and European separators with a decimal comma (``9.999,5``) or two or
more dot groups (``1.000.000``). A lone ``12,34`` is ambiguous and is not read. Number words
without a scale word (``one``, ``two``) are prose and are not checked.
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

from evalkit.schemas import Case, Completion, ResultSet
from evalkit.scorers.base import ScoreResult, scorer

REL_TOL = 0.005  # 0.5% relative (agentic-analytics Tech Spec §6.1)
MAX_RATIO_PAIRS = 20_000  # cell pairs tried when deriving an `x` multiple
MAX_NUMBER_WORDS = 12  # longest run of number words read as one number
_MULTIPLE_WINDOW = 200  # characters each side of an `x` multiple searched for row names

Kind = Literal["num", "pct"]

_SAME_LENGTH = str.maketrans(
    {
        "−": "-",  # minus sign
        "–": "-",  # en dash
        "—": "-",  # em dash
        "‐": "-",
        "‑": "-",
        "’": "'",
        " ": " ",
        " ": " ",
        " ": " ",
        " ": " ",
    }
)

_EURO = r"\d{1,3}(?:\.\d{3})+,\d+|\d{1,3}(?:\.\d{3}){2,}"
_INDIAN = r"\d{1,2}(?:,\d{2})+,\d{3}"
_STD = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_CORE = re.compile(rf"(?<![A-Za-z0-9_.,])(?:{_EURO}|{_INDIAN}|{_STD})(?![0-9]|[.,][0-9])")
_EURO_RE = re.compile(_EURO)
_PERCENT = re.compile(r"\s?(?:%|per\s?cent\b|percentage\s+points?\b|pp\b|ppts?\b)", re.IGNORECASE)
_WORD_SCALE = re.compile(r"\s?(thousand|million|billion|mn|bn)\b", re.IGNORECASE)
_LETTER_SCALE = re.compile(r"(k|K|mn|m|M|bn|b|B)(?![A-Za-z0-9²³])")
_RATIO = re.compile(r"x(?![A-Za-z0-9])")
_DECADE = re.compile(r"s(?![A-Za-z0-9])")
_ORDINAL = re.compile(r"(?:st|nd|rd|th)(?![A-Za-z])", re.IGNORECASE)
_APPROX_SUFFIX = re.compile(r"-(?:plus|odd|ish)\b", re.IGNORECASE)
_RANGE_GAP = re.compile(r"\s*(?:-|to|and)\s*", re.IGNORECASE)
_SCALES = {
    "k": 1e3,
    "thousand": 1e3,
    "m": 1e6,
    "mn": 1e6,
    "million": 1e6,
    "b": 1e9,
    "bn": 1e9,
    "billion": 1e9,
}

_UNITS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
    "thirteen": 13,
    "fourteen": 14,
    "fifteen": 15,
    "sixteen": 16,
    "seventeen": 17,
    "eighteen": 18,
    "nineteen": 19,
    "twenty": 20,
    "thirty": 30,
    "forty": 40,
    "fifty": 50,
    "sixty": 60,
    "seventy": 70,
    "eighty": 80,
    "ninety": 90,
}
_WORD_SCALES = {"hundred": 100.0, "thousand": 1e3, "million": 1e6, "billion": 1e9}
_LINKS = ("a", "an", "and", "of")
_VOCAB = set(_UNITS) | set(_WORD_SCALES) | set(_LINKS) | {"half", "quarter"}
_WORD = re.compile(r"[A-Za-z]+")

# Spans that are not number claims, blanked with spaces so positions stay put.
_MONTHS = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
    r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_CODE_FENCE = re.compile(r"```.*?(?:```|\Z)", re.DOTALL)
_LIST_NUMBER = re.compile(r"(?m)^(\s*)(\d{1,2}[.)])(?=\s)")
_TABLE_RANK = re.compile(r"(?m)^(\s*\|\s*)(\d{1,2})(?=\s*\|[^\n]*\S)")
_HASH_RANK = re.compile(r"(?:#\d+|\[\d+\])")
_TIME = re.compile(r"(?<![\d.])\d{1,2}:\d{2}(?::\d{2})?(?![\d])")
_CIRCA = re.compile(r"\bc\.(?=\s?[£$€\d])")
_CURRENCY_CODE = re.compile(r"\b(?:GBP|USD|EUR)(?=\s?\d)")
_ISO_DATE = re.compile(r"(?<![\d.,])((?:19|20)\d{2})(-\d{1,2}-\d{1,2})(?![\d])")
_DMY_DATE = re.compile(r"(?<![\d.,])(\d{1,2}[/.]\d{1,2}[/.])((?:19|20)\d{2})(?![\d])")
_SHORT_DMY = re.compile(r"(?<![\d.,/])\d{1,2}/\d{1,2}/\d{2}(?![\d/])")
_YEAR_PART = re.compile(r"(?<![\d.,])((?:19|20)\d{2})([-/]\d{2})(?![\d]|[-/.]\d)")
_DAY_MONTH = re.compile(
    rf"(?<![\d.,])\d{{1,2}}(?:st|nd|rd|th)?(?=\s+(?:of\s+)?{_MONTHS}\b)", re.IGNORECASE
)
_MONTH_DAY = re.compile(
    rf"(\b{_MONTHS}\.?\s+)(\d{{1,2}}(?:st|nd|rd|th)?)(?![\d]|[.,]\d)", re.IGNORECASE
)
_ABBR_YEAR = re.compile(r"(?<![\w])'\d{2}(?![\d])")

_FRACTION_NAMES = re.compile(r"share|ratio|fraction|proportion|(?:^|_)rate(?:$|_)", re.I)
_PERCENT_NAMES = re.compile(r"pct|percent", re.IGNORECASE)


@dataclass(frozen=True)
class Number:
    """One number read from text."""

    text: str  # as written, e.g. "£254k"
    start: int
    # (absolute value, half a displayed unit, kind); a "pct" value is in percent units
    candidates: tuple[tuple[float, float, Kind], ...]
    plain_int: bool  # an integer with no currency, scale, percent, sign or decimals
    year_like: bool  # a bare four-digit integer
    decade: bool = False  # "1990s"
    multiple: bool = False  # "6.6x"
    negative: bool = False  # written with a leading minus ("-3.2%")

    @property
    def value(self) -> float:
        return self.candidates[0][0]


def _blank(text: str, pattern: re.Pattern[str], keep: Iterable[int] = ()) -> str:
    """Replace every match with spaces, except the groups named in `keep`."""
    kept = tuple(keep)

    def sub(m: re.Match[str]) -> str:
        if not kept:
            return " " * len(m.group(0))
        out = []
        for i in range(1, (m.lastindex or 0) + 1):
            part = m.group(i) or ""
            out.append(part if i in kept else " " * len(part))
        return "".join(out)

    return pattern.sub(sub, text)


def _prepare(text: str) -> str:
    t = text.translate(_SAME_LENGTH)
    t = _blank(t, _CODE_FENCE)
    t = _blank(t, _LIST_NUMBER, keep=(1,))
    t = _blank(t, _TABLE_RANK, keep=(1,))
    t = _blank(t, _HASH_RANK)
    t = _blank(t, _CIRCA)
    t = _blank(t, _CURRENCY_CODE)
    t = _blank(t, _ISO_DATE, keep=(1,))
    t = _blank(t, _DMY_DATE, keep=(2,))
    t = _blank(t, _SHORT_DMY)
    t = _blank(t, _YEAR_PART, keep=(1,))
    t = _blank(t, _DAY_MONTH)
    t = _blank(t, _MONTH_DAY, keep=(1,))
    t = _blank(t, _ABBR_YEAR)
    return _blank(t, _TIME)


@dataclass
class _Raw:
    start: int
    end: int
    value: float
    half: float
    percent: bool = False
    mult: float = 1.0
    plain_int: bool = False
    year_like: bool = False
    decade: bool = False
    multiple: bool = False
    negative: bool = False


def _parse_core(core: str) -> tuple[float, int]:
    """(value, digits after the decimal mark) for a standard, Indian or European figure."""
    if _EURO_RE.fullmatch(core):
        whole, _, frac = core.replace(".", "").partition(",")
    else:
        whole, _, frac = core.replace(",", "").partition(".")
    return float(whole + ("." + frac if frac else "")), len(frac)


def _core_numbers(t: str) -> list[_Raw]:
    out: list[_Raw] = []
    for m in _CORE.finditer(t):
        core = m.group(0)
        base, decimals = _parse_core(core)
        unit = 10.0**-decimals
        start = m.start()
        currency = start > 0 and t[start - 1] in "£$€"
        if currency:
            start -= 1
        end = m.end()
        rest = t[end : end + 40]  # every suffix is short; a bounded window keeps this linear
        mult, percent, decade, multiple = 1.0, False, False, False
        if p := _PERCENT.match(rest):
            percent, end = True, end + p.end()
        elif w := _WORD_SCALE.match(rest):
            mult, end = _SCALES[w.group(1).lower()], end + w.end()
        elif s := _LETTER_SCALE.match(rest):
            mult, end = _SCALES[s.group(1).lower()], end + s.end()
        elif r := _RATIO.match(rest):
            multiple, end = True, end + r.end()
        elif (
            (d := _DECADE.match(rest))
            and len(core) == 4
            and core.isdigit()
            and t[m.start() - 1 : m.start()] != "'"
        ):
            decade, end = True, end + d.end()
        elif _ORDINAL.match(rest):
            continue  # 3rd, 21st
        elif rest[:1].isalpha():
            if core.isdigit() and len(core) <= 2:
                continue  # 2bed, 10am; 999homes stays a claim
        elif _APPROX_SUFFIX.match(rest):
            pass  # 260,000-odd is a claim
        elif (
            rest[:1] == "-"
            and rest[1:2].isalpha()
            and core.isdigit()
            and len(core) <= 3
            and not currency
        ):
            continue  # 10-year high
        negative = start > 0 and t[start - 1] == "-" and (start < 2 or not t[start - 2].isalnum())
        if negative:
            start -= 1
        plain = not (currency or negative or percent or decimals or mult != 1.0 or end > m.end())
        out.append(
            _Raw(
                start=start,
                end=end,
                value=base * mult,
                half=unit * mult / 2,
                percent=percent,
                mult=mult,
                plain_int=plain,
                year_like=plain and len(core) == 4 and core.isdigit(),
                decade=decade,
                multiple=multiple,
                negative=negative,
            )
        )
    return out


def _fraction_words(words: list[str]) -> tuple[float, float] | None:
    """'half a million', 'a quarter of a million'."""
    scales = [w for w in words if w in _WORD_SCALES]
    if len(scales) != 1 or any(w in _UNITS for w in words):
        return None
    scale = _WORD_SCALES[scales[0]]
    if "half" in words:
        return 0.5 * scale, scale / 10
    return 0.25 * scale, scale / 100


def _evaluate_words(words: list[str]) -> tuple[float, float] | None:
    """(value, displayed unit) of a run of number words, or None if it is not a number."""
    if not any(w in _WORD_SCALES for w in words):
        return None
    if "half" in words or "quarter" in words:
        if words[0] in ("half", "quarter") or words[:2] in (["a", "quarter"], ["an", "quarter"]):
            return _fraction_words(words)
        return None
    total = current = 0.0
    seen_number = False
    for i, w in enumerate(words):
        if w in _UNITS:
            current += _UNITS[w]
            seen_number = True
        elif w in ("a", "an"):
            if i + 1 < len(words) and words[i + 1] in _WORD_SCALES:
                current += 1
                seen_number = True
            else:
                return None
        elif w == "hundred":
            current = (current or 1) * 100
        elif w in _WORD_SCALES:
            total += (current or 1) * _WORD_SCALES[w]
            current = 0.0
        elif w == "of":
            return None
    value = total + current
    if not seen_number or value <= 0:
        return None
    if not math.isfinite(value):
        return None
    zeros = 0
    while zeros < 30 and value % 10 ** (zeros + 1) == 0:
        zeros += 1
    last_scale = next(_WORD_SCALES[w] for w in reversed(words) if w in _WORD_SCALES)
    return value, min(10.0**zeros, last_scale)  # "twenty thousand" reads like "20k"


def _word_numbers(t: str) -> list[_Raw]:
    """Runs of number words that carry a scale word, e.g. 'four thousand one hundred'."""
    out: list[_Raw] = []
    runs: list[list[re.Match[str]]] = []
    for m in _WORD.finditer(t):
        if m.group(0).lower() not in _VOCAB:
            continue
        if runs and re.fullmatch(r"[\s-]+", t[runs[-1][-1].end() : m.start()]):
            runs[-1].append(m)
        else:
            runs.append([m])
    for run in runs:
        words = [m.group(0).lower() for m in run]
        i = 0
        while i < len(run):
            if words[i] in ("and", "of"):
                i += 1
                continue
            # longest run from i first, at most MAX_NUMBER_WORDS words: linear in the text
            for j in range(min(len(run), i + MAX_NUMBER_WORDS), i, -1):
                k = j
                while k > i and words[k - 1] in _LINKS:
                    k -= 1
                got = _evaluate_words(words[i:k]) if k > i else None
                if got is not None:
                    value, unit = got
                    out.append(_Raw(run[i].start(), run[k - 1].end(), value, unit / 2, mult=unit))
                    i = k - 1
                    break
            i += 1
    return out


def extract_numbers(text: str) -> list[Number]:
    """Every number claim in `text`, in reading order."""
    t = _prepare(text)
    raws = sorted(_core_numbers(t) + _word_numbers(t), key=lambda r: r.start)
    out: list[Number] = []
    for i, r in enumerate(raws):
        kind: Kind = "pct" if r.percent else "num"
        cands: list[tuple[float, float, Kind]] = [(abs(r.value), r.half, kind)]
        nxt = raws[i + 1] if i + 1 < len(raws) else None
        if (
            nxt is not None
            and not (r.mult != 1.0 or r.percent)
            and (nxt.mult != 1.0 or nxt.percent)
            and _RANGE_GAP.fullmatch(t[r.end : nxt.start])
        ):
            # "£1.2-1.3m" or "10-12%": the first figure may carry the second's suffix
            if nxt.percent:
                cands.append((abs(r.value), r.half, "pct"))
            else:
                cands.append((abs(r.value) * nxt.mult, r.half * nxt.mult, "num"))
        out.append(
            Number(
                text=t[r.start : r.end].strip(),
                start=r.start,
                candidates=tuple(cands),
                plain_int=r.plain_int,
                year_like=r.year_like,
                decade=r.decade,
                multiple=r.multiple,
                negative=r.negative and r.value != 0,  # "-0" is zero
            )
        )
    return out


@dataclass
class _Pool:
    """Sorted absolute values of cells, by how a number may match them."""

    every: list[float] = field(default_factory=list)  # all numeric cells
    percent_units: list[float] = field(default_factory=list)  # cells read as percent units
    fractions: list[float] = field(default_factory=list)  # cells read as fractions (<= 1)
    # numbers written inside text cells and column names ("FLAT 12", "SALES 5000")
    in_text: list[float] = field(default_factory=list)

    def sort(self) -> None:
        for vals in (self.every, self.percent_units, self.fractions, self.in_text):
            vals.sort()


@dataclass
class _Cells:
    any_sign: _Pool = field(default_factory=_Pool)  # every cell; backs an unsigned number
    negative: _Pool = field(default_factory=_Pool)  # negative cells; back a written minus
    grid: list[list[float | None]] = field(default_factory=list)  # signed numeric cells
    row_names: dict[str, int] = field(default_factory=dict)  # word -> the one row it names
    column_names: list[frozenset[str]] = field(default_factory=list)  # words per numeric column
    numeric_columns: list[int] = field(default_factory=list)


_NAME_WORD = re.compile(r"[A-Za-z]{3,}")
_NAME_STOP = frozenset({"and", "the", "for", "with", "from", "per"})
_BOUNDARY = re.compile(r"[.!?;](?=\s|$)|\n")


def _name_words(text: str) -> set[str]:
    return {w.lower() for w in _NAME_WORD.findall(text)} - _NAME_STOP


def _cell_number(cell: object) -> float | None:
    """A cell's signed numeric value, or None for text, booleans and missing values."""
    if isinstance(cell, bool) or cell is None:
        return None
    v: float | None = None
    if isinstance(cell, int | float):
        v = float(cell)
    elif isinstance(cell, str) and re.fullmatch(r"\s*-?\d[\d,]*(?:\.\d+)?\s*", cell):
        try:
            v = float(cell.replace(",", ""))
        except ValueError:
            return None
    return v if v is not None and math.isfinite(v) else None


def _cells(rs: ResultSet | None) -> _Cells:
    out = _Cells()
    if rs is None:
        return out
    fraction_col = [bool(_FRACTION_NAMES.search(c)) for c in rs.columns]
    percent_col = [bool(_PERCENT_NAMES.search(c)) for c in rs.columns]
    numeric_col = [False] * len(rs.columns)
    texts: set[str] = set(rs.columns)
    rows_of: dict[str, set[int]] = {}
    for i, row in enumerate(rs.rows):
        line: list[float | None] = []
        for j, cell in enumerate(row):
            signed = _cell_number(cell)
            line.append(signed)
            if signed is None:
                if isinstance(cell, str):
                    texts.add(cell)
                    for w in _name_words(cell):
                        rows_of.setdefault(w, set()).add(i)
                continue
            numeric_col[j] = True
            v = abs(signed)
            pools = (out.any_sign, out.negative) if signed < 0 else (out.any_sign,)
            for pool in pools:
                pool.every.append(v)
                if v <= 1 and (fraction_col[j] or percent_col[j]):
                    pool.fractions.append(v)
                    if percent_col[j] and not fraction_col[j]:
                        pool.percent_units.append(v)
                else:
                    pool.percent_units.append(v)
        out.grid.append(line)
    out.row_names = {w: next(iter(rows)) for w, rows in rows_of.items() if len(rows) == 1}
    out.numeric_columns = [j for j, numeric in enumerate(numeric_col) if numeric]
    out.column_names = [
        frozenset(_name_words(c)) if numeric_col[j] else frozenset()
        for j, c in enumerate(rs.columns)
    ]
    for text in sorted(texts):
        for n in extract_numbers(text):
            out.any_sign.in_text.extend(_values(n))
            if n.negative:
                out.negative.in_text.extend(_values(n))
    out.any_sign.sort()
    out.negative.sort()
    return out


def _in_text(n: Number, cells: _Cells) -> bool:
    """The number is written, token for token, inside a text cell or a column name."""
    in_text = (cells.negative if n.negative else cells.any_sign).in_text
    for x in _values(n):
        eps = 1e-9 * max(1.0, x)
        i = bisect.bisect_left(in_text, x - eps)
        if i < len(in_text) and in_text[i] <= x + eps:
            return True
    return False


def _near(x: float, half: float, cells: list[float], rel_tol: float) -> bool:
    eps = 1e-9 * max(1.0, x)
    w = max(half, rel_tol * x / (1 - rel_tol)) + eps
    lo, hi = bisect.bisect_left(cells, x - w), bisect.bisect_right(cells, x + w)
    return any(_within(v, x, half, rel_tol) for v in cells[lo:hi])


def _within(v: float, x: float, half: float, rel_tol: float) -> bool:
    eps = 1e-9 * max(1.0, x)
    d = abs(v - x)
    return d <= half + eps or d <= rel_tol * max(v, x) + eps


def _matches_cells(n: Number, cells: _Cells, rel_tol: float) -> bool:
    pool = cells.negative if n.negative else cells.any_sign
    for x, half, kind in n.candidates:
        if kind == "num" and _near(x, half, pool.every, rel_tol):
            return True
        if kind == "pct" and (
            _near(x, half, pool.percent_units, rel_tol)
            or _near(x / 100, half / 100, pool.fractions, rel_tol)
        ):
            return True
    return False


def _derived_multiple(n: Number, text: str, cells: _Cells, rel_tol: float) -> bool:
    """`n` is an `x` multiple and its clause names two rows whose cells in one numeric column
    (one named in the clause, if any is) have that ratio to the precision displayed. Rows named
    before the multiple are the numerator and rows after it the denominator; with all names on
    one side, either order. Nothing is derived without two named rows."""
    if not (n.multiple and cells.row_names) or n.negative:
        return False
    start, end = n.start, n.start + len(n.text)
    before = text[max(0, start - _MULTIPLE_WINDOW) : start]
    after = text[end : end + _MULTIPLE_WINDOW]
    cut = [m.end() for m in _BOUNDARY.finditer(before)]
    if cut:
        before = before[cut[-1] :]
    if stop := _BOUNDARY.search(after):
        after = after[: stop.start()]
    words_before, words_after = _name_words(before), _name_words(after)
    rows_before = {cells.row_names[w] for w in words_before if w in cells.row_names}
    rows_after = {cells.row_names[w] for w in words_after if w in cells.row_names}
    if rows_before and rows_after:
        pairs = [(a, b) for a in sorted(rows_before) for b in sorted(rows_after) if a != b]
    else:
        named = sorted(rows_before | rows_after)
        pairs = [(a, b) for a in named for b in named if a != b]
    words = words_before | words_after
    named_columns = [j for j, names in enumerate(cells.column_names) if names & words]
    columns = named_columns or cells.numeric_columns
    if not pairs or len(pairs) * len(columns) > MAX_RATIO_PAIRS:
        return False
    x, half, _ = n.candidates[0]
    for a, b in pairs:
        for j in columns:
            va, vb = cells.grid[a][j], cells.grid[b][j]
            if va is None or vb is None or va == 0 or vb == 0 or (va < 0) != (vb < 0):
                continue
            if _within(va / vb, x, half, rel_tol):
                return True
    return False


def _values(n: Number) -> set[float]:
    out: set[float] = set()
    for x, _, kind in n.candidates:
        out.add(x)
        if kind == "pct":
            out.add(x / 100)
    return out


def _same_value(a: Number, b: Number) -> bool:
    return any(
        math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-12) for x in _values(a) for y in _values(b)
    )


def _in_years(n: Number, year_range: tuple[int, int] | None) -> bool:
    if year_range is None:
        return False
    lo, hi = year_range
    if n.decade:
        return n.value <= hi and n.value + 9 >= lo
    return n.year_like and lo <= n.value <= hi


def _finite(n: Number) -> bool:
    return all(math.isfinite(x) and math.isfinite(h) for x, h, _ in n.candidates)


def unsupported_numbers(
    answer: str,
    rows: ResultSet | None,
    question: str = "",
    *,
    top_n: int | None = None,
    year_range: tuple[int, int] | None = None,
    rel_tol: float = REL_TOL,
) -> tuple[str, ...]:
    """The numbers in `answer` that no rule above supports, as written, first-seen order, each
    once. `rows` is None when nothing executed; then only the question, years, top-N and a row
    count of zero can support a number. A number too large to represent is never supported."""
    return _check(answer, rows, question, top_n, year_range, rel_tol)[0]


def _check(
    answer: str,
    rows: ResultSet | None,
    question: str,
    top_n: int | None,
    year_range: tuple[int, int] | None,
    rel_tol: float,
) -> tuple[tuple[str, ...], int]:
    if not 0 <= rel_tol < 1:
        raise ValueError("rel_tol must be in [0, 1)")
    if year_range is not None and (len(year_range) != 2 or year_range[0] > year_range[1]):
        raise ValueError("year_range must be (first, last) with first <= last")
    cells = _cells(rows)
    n_rows = len(rows.rows) if rows is not None else 0
    asked = extract_numbers(question)
    numbers = extract_numbers(answer)
    left: list[str] = []
    for n in numbers:
        if _finite(n):
            if _in_years(n, year_range):
                continue
            if n.plain_int and (n.value == top_n or n.value == n_rows):
                continue
            if any(_same_value(n, q) for q in asked):
                continue
            if (
                _matches_cells(n, cells, rel_tol)
                or _in_text(n, cells)
                or _derived_multiple(n, answer, cells, rel_tol)
            ):
                continue
        if n.text not in left:
            left.append(n.text)
    return tuple(left), len(numbers)


def answer_from_rows(
    answer: str,
    rows: ResultSet | None,
    question: str = "",
    *,
    top_n: int | None = None,
    year_range: tuple[int, int] | None = None,
    rel_tol: float = REL_TOL,
) -> ScoreResult:
    """1.0 when every number in `answer` is backed by `rows` (or the question, a year in
    `year_range`, `top_n`, the row count), else 0.0. Details: `unsupported`, `n_numbers`."""
    left, n = _check(answer, rows, question, top_n, year_range, rel_tol)
    return ScoreResult(
        value=0.0 if left else 1.0, details={"unsupported": list(left), "n_numbers": n}
    )


def _meta_rows(value: Any) -> ResultSet | None:
    if value is None:
        return None
    if isinstance(value, ResultSet):
        return value
    if not isinstance(value, Mapping):
        raise ValueError("case.meta['result'] must be an object with columns and rows")
    return ResultSet.model_validate(
        {k: value[k] for k in ("columns", "rows", "ordered") if k in value}
    )


@scorer("answer_from_rows")
def _answer_from_rows_task(case: Case, completion: Completion, **_: Any) -> ScoreResult:
    """Task scorer: the answer text against `case.meta["result"]` ({"columns", "rows"}), with
    `case.meta["top_n"]` and `case.meta["year_range"]` when given. Failed calls score 0.0."""
    if completion.error is not None or completion.answer is None:
        return ScoreResult(value=0.0, details={"reason": completion.error or "no_answer"})
    meta = case.meta
    years = meta.get("year_range")
    top_n = meta.get("top_n")
    return answer_from_rows(
        completion.answer.answer,
        _meta_rows(meta.get("result")),
        case.question,
        top_n=top_n if isinstance(top_n, int) and not isinstance(top_n, bool) else None,
        year_range=(int(years[0]), int(years[1])) if years is not None else None,
    )
