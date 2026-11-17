"""Opt-in shape equivalences for `sql_match` (SQE-01, SQE-02; D-71, B5.15).

Lifted from agentic-analytics (MIT, same author), `src/agentic_analytics/scoring/tolerant.py`
at commit f6abdec ("OWNER D-81: tolerant headline scoring"), with its text period labels
(7b3aded, OWNER D-89) and leading-word count rule (5a203a7) added later, adapted to `sql_match`'s
arguments (the question is plain text; naming `empty_check` stands for agentic-analytics'
`expects_empty`) and made stricter where the reference can turn a wrong answer into a match
(B5.15 verifier): an equivalence may only reshape a right answer.

Each rule is a pre-normalisation of the predicted rows, tried only after the strict match
failed and only when the caller names it; the normalised rows must then pass the strict match
(same tolerance, same column handling, same row count, `gold.ordered`). The gold is never
changed. A column a rule converted must be compared with a gold column of the kind it was
converted for (a scaled fraction with a percent column, a month number with a month column);
any other column mapping fails. Column names are read as whole words, case-insensitive, split
at ``_``, ``-``, spaces, other punctuation and letter/digit changes (``NEW_BUILD_PCT`` is new,
build, pct; ``q1`` is q, 1). A name that must *name* something (a percent, month, quarter or
count column) is not split at case changes (``newBuildPct`` is one word, not a percent
column); a word that *excludes* a column (a summary word, a measure) is also found at case
changes (``avgPrice`` has avg). Where agentic-analytics' D-91 rules (a638db9) and these
differ, the stricter is kept.

1. **percent_scale.** The question asks for a percentage: it says "percent", "percentage",
   "per cent" or "%" ("percentile" is not one) and nowhere negates one ("not %", "not a
   percentage", "no percent", "rather than as a percent", "instead of a percentage"). A gold
   column is named as a percentage: a word ``pct``, ``percent`` or ``percentage`` in its name
   (not ``percentile``, ``pcnt``, ``rate``, ``share`` or ``ratio``), or both words ``points``
   and ``change`` (``change_points``) when the question asks for percentage points
   (``avg_loyalty_points`` is not one). A predicted numeric column is multiplied by 100 when,
   so scaled, its values fit such a gold column and, unscaled, they do not (0.0817 for 8.17).
   The scaled column alone is compared with a percent-unit tolerance, `abs` = max(`abs_tol`,
   0.01) with the caller's `rel_tol` (agentic-analytics D-51's per-column tolerance); every
   other column keeps the caller's `abs_tol`.
2. **pivot.** The gold is long: two columns, one all-text label column and one all-numeric
   value column, at least one row, distinct labels. The prediction is one wide row whose
   column names are the gold's labels (case-insensitive; spaces, ``-`` and ``_`` are the same
   separator). Every gold label must be a predicted column. Any other predicted column must
   hold a numeric zero and look like a label the gold's GROUP BY omitted (``unknown = 0``):
   no summary or metric word in its name (``SUMMARY_WORDS``: total, sum, all, overall, count,
   n, avg, pct, share, ...), not one of the gold's own column names, and written only with the
   kinds of character the gold's labels use (letters, digits, separators, anything else) and
   starting with a digit when any label does, so ``unknown`` beside year labels, or beside
   ``2023-03-01T00:00:00Z`` or ``2023 Q1``, is not one. The row is unpivoted into the gold's
   columns.
3. **empty_check.** With an empty gold, a single predicted row counts as no rows when it is a
   zero count with nothing but dates and nulls beside it: at least one number, every number
   (numbers, and text that is a plain number) zero and in a count column, and every other
   cell null or an ISO date or timestamp (``YYYY-MM-DD[ hh:mm[:ss[.f]]][zone]``), as in
   ``earliest_sale_date, latest_sale_date, sales_in_1994 = 0``. A count column has a word of
   ``COUNT_WORDS`` (count, cnt, n, num, number, rows, records, sales, transactions, orders,
   items) in its name and none of
   ``NOT_COUNT_WORDS`` (avg, mean, median, min, max, sum, price, amount, value, revenue, pct,
   ratio, rate, share, ...), and its leading word is not one of ``NOT_A_COUNT_LEADS`` (year,
   q, quarter, month, day, id, rank, total, grand; agentic-analytics D-91): ``sales_in_1994``
   and ``sales_in_requested_year`` count, ``total_sales``, ``year_count``, ``total`` and
   ``total_revenue`` do not. An error message, a zero average or a zero total is not a
   no-data check.
4. **period_number.** A predicted column of period starts in one calendar year becomes the
   month number (1-12) when the question names months and a gold column named as a month (a
   word ``month``) holds integers 1-12, or the quarter number (1-4) when every period starts a
   quarter, the question names quarters and a gold column named as a quarter (a word
   ``quarter`` or ``qtr``) holds integers 1-4; the converted column must be compared with such
   a column. A period start is a month-start date (``YYYY-MM-01``, optionally with a midnight
   time), a month label (``2023-03``, ``March 2023``, ``Mar 2023``, ``2023 March``) or a
   quarter label (``2023 Q1``, ``Q1 2023``, ``2023-Q1``, ``Q1``; agentic-analytics OWNER
   D-89). A quarter label never becomes a month number: when any converted column holds one,
   there is no month reading. ``Q5``, ``2023-13`` and week labels (``2023-W05``) are not
   period starts. When the question names a year (four digits 19xx or 20xx not inside a
   longer number), the periods' year must be one it names. A bare ``Q1`` states no year: it is
   read only when the question names exactly one year, as that year. A question about fiscal
   periods (``FY2024``, ``FY 24``, "fiscal") never converts: a fiscal quarter need not be a
   calendar one. Periods over two or more years never convert: their numbers would merge
   different periods.

percent_scale and period_number act on columns and may apply together (a monthly percentage
change returned as month dates and fractions); pivot and empty_check reshape the whole result
and apply alone.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from evalkit.schemas import ResultSet

EQUIVALENCES: tuple[str, ...] = ("percent_scale", "pivot", "empty_check", "period_number")

PERCENT_ABS = 0.01  # a percent-unit column's absolute tolerance (agentic-analytics D-51)
PERCENT_SCALE = 100
_PERCENT_WORD = r"(?:%|\bper\s?cent(?:age)?s?\b)"  # "percentile" is not one
_ASKS_PERCENT = re.compile(_PERCENT_WORD, re.IGNORECASE)
_NOT_PERCENT = re.compile(
    r"\b(?:not|no|never|without|rather\s+than|instead\s+of|non-?)\s*"
    r"(?:(?:a|an|as|in|into|the|expressed|given|shown|returned|reported)\s+){0,3}" + _PERCENT_WORD,
    re.IGNORECASE,
)
_ASKS_POINTS = re.compile(r"\bper\s?cent(?:age)?[\s-]+points?\b", re.IGNORECASE)
PERCENT_WORDS = frozenset({"pct", "percent", "percentage"})
POINT_CHANGE_WORDS = frozenset({"points", "change"})  # a percentage-point change
_NAMES_MONTHS = re.compile(r"\bmonths?\b", re.IGNORECASE)
_NAMES_QUARTERS = re.compile(r"\bquarters?\b", re.IGNORECASE)
_NAMES_YEAR = re.compile(r"(?<![0-9])((?:19|20)[0-9]{2})(?![0-9])")
MONTH_WORDS = frozenset({"month"})
QUARTER_WORDS = frozenset({"quarter", "qtr"})
# a name with these words is a count or a sub-period, not a month or quarter number
NOT_A_PERIOD_WORDS = frozenset(
    {"day", "days", "week", "weeks", "count", "n", "num", "number", "total", "sum", "avg"}
)
_PERIOD_START = re.compile(
    r"(\d{4})-(\d{2})-01(?:[T ]00:00(?::00(?:\.0+)?)?(?:Z|[+-]00:?00)?)?", re.ASCII
)
_ISO_DATE = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?",
    re.ASCII,
)
_PLAIN_NUMBER = re.compile(r"\s*[-+]?\d+(?:\.\d+)?\s*", re.ASCII)
_SEPARATORS = re.compile(r"[\s_-]+")
_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
_WORD = re.compile(r"[a-z]+|[0-9]+")
COUNT_WORDS = frozenset(
    {"count", "counts", "cnt", "n", "num", "number", "rows", "records"}
    | {"sales", "transactions", "orders", "items"}
)
NOT_COUNT_WORDS = frozenset(
    {"avg", "average", "mean", "median", "min", "max", "sum", "price", "amount", "value"}
    | {"revenue", "pct", "percent", "percentage", "ratio", "rate", "share"}
)
# a count column may not lead with these: it is a period, an id, a rank or a total, not a count
# of the rows asked for (agentic-analytics D-91; ``sales_in_requested_year`` stays a count)
NOT_A_COUNT_LEADS = frozenset(
    {"year", "q", "quarter", "month", "day", "id", "rank", "total", "grand"}
)
SUMMARY_WORDS = frozenset(
    {"total", "totals", "subtotal", "sum", "all", "overall", "grand", "count", "counts", "cnt"}
    | {"n", "num", "avg", "average", "mean", "median", "min", "max", "pct", "percent"}
    | {"percentage", "share", "ratio", "rate"}
)
QUARTER_STARTS = frozenset({1, 4, 7, 10})

Converted = dict[int, tuple[list[Any], frozenset[int]]]  # column: (new values, gold targets)


@dataclass(frozen=True)
class Variant:
    """A normalised form of the predicted rows, with the rules that made it. `bound` maps each
    converted predicted column to the gold columns it may be compared with; `percent` lists
    the converted columns compared with the percent-unit tolerance."""

    rules: tuple[str, ...]
    predicted: ResultSet
    bound: Mapping[int, frozenset[int]] = field(default_factory=dict)
    percent: frozenset[int] = frozenset()


def check_names(equivalences: Collection[str]) -> frozenset[str]:
    """The named equivalences as a set; a bare string or an unknown name is an error."""
    if isinstance(equivalences, str | bytes):
        raise TypeError("equivalences must be a collection of names, not a single string")
    names = frozenset(equivalences)
    unknown = sorted(str(n) for n in names - set(EQUIVALENCES))
    if unknown:
        raise ValueError(f"unknown sql_match equivalences {unknown}; known: {list(EQUIVALENCES)}")
    return names


def words(name: str) -> frozenset[str]:
    """A column name's whole words, case-insensitive, not split at case changes: the words
    that may name a column (``NEW_BUILD_PCT`` -> new, build, pct; ``newBuildPct`` ->
    newbuildpct)."""
    return frozenset(_WORD.findall(name.casefold()))


def any_words(name: str) -> frozenset[str]:
    """`words` plus the words found at case changes (``avgPrice`` -> avgprice, avg, price):
    the words that may exclude a column."""
    return words(name) | frozenset(_WORD.findall(_CAMEL.sub(" ", name).casefold()))


def _is_num(v: Any) -> bool:
    return isinstance(v, int | float | Decimal) and not isinstance(v, bool)


def _close(a: float, b: float, rel_tol: float, abs_tol: float) -> bool:
    if not (math.isfinite(a) and math.isfinite(b)):
        return False  # an overflowed x100 must not fit everything by an infinite tolerance
    d = abs(a - b)
    return d <= abs_tol or d <= rel_tol * max(abs(a), abs(b))


class _Column:
    """What a column's values must agree on for any row matching to exist."""

    def __init__(self, values: Sequence[Any]) -> None:
        self.others = Counter(
            v.rstrip() if isinstance(v, str) else v for v in values if not _is_num(v)
        )
        self.sorted_nums = sorted(float(v) for v in values if _is_num(v))

    def fits(self, gold: _Column, rel_tol: float, abs_tol: float) -> bool:
        if self.others != gold.others or len(self.sorted_nums) != len(gold.sorted_nums):
            return False
        pairs = zip(self.sorted_nums, gold.sorted_nums, strict=True)
        return all(_close(a, b, rel_tol, abs_tol) for a, b in pairs)


def variants(
    predicted: ResultSet,
    gold: ResultSet,
    names: Collection[str],
    question: str | None,
    rel_tol: float,
    abs_tol: float,
) -> Iterator[Variant]:
    """Each normalised form of `predicted` the named rules allow, in the order tried."""
    if "empty_check" in names and (empty := empty_check(predicted, gold)) is not None:
        yield Variant(("empty_check",), empty)
    if "pivot" in names and (long := pivot(predicted, gold)) is not None:
        yield Variant(("pivot",), long)
    scaled: Converted = {}
    periods: list[Converted] = []
    if "percent_scale" in names and question is not None:
        scaled = percent_columns(predicted, gold, question, rel_tol, abs_tol)
    if "period_number" in names and question is not None:
        periods = period_columns(predicted, gold, question)
    pct = frozenset(scaled)
    if scaled:
        yield _variant(("percent_scale",), predicted, scaled, pct)
    for period in periods:
        yield _variant(("period_number",), predicted, period, frozenset())
    if scaled:
        for period in periods:
            both = {**scaled, **period}
            yield _variant(("percent_scale", "period_number"), predicted, both, pct)


def _variant(
    rules: tuple[str, ...], rs: ResultSet, columns: Converted, percent: frozenset[int]
) -> Variant:
    rows = tuple(
        tuple(columns[i][0][k] if i in columns else v for i, v in enumerate(row))
        for k, row in enumerate(rs.rows)
    )
    bound = {i: targets for i, (_, targets) in columns.items()}
    return Variant(rules, ResultSet(columns=rs.columns, rows=rows), bound, percent)


def _column(rs: ResultSet, i: int) -> list[Any]:
    return [row[i] for row in rs.rows]


# 1. percent_scale


# Fail-safe (B5.15 verifier pass 2): any negation in a clause that mentions a percentage
# ("do not express it as a percentage", "avoid percentages", "without converting to a
# percentage") means the question does not ask for one.
_ANY_NEGATION = re.compile(
    r"\b(?:not|no|never|without|avoid|avoiding|instead|rather|nor|neither)\b|n['’]t\b",
    re.IGNORECASE,
)


def asks_percent(question: str) -> bool:
    """The question asks for a percentage and no clause that mentions one negates anything."""
    if not _ASKS_PERCENT.search(question) or _NOT_PERCENT.search(question):
        return False
    # the clause that mentions the percentage ("Not counting refunds, what percentage ...?"
    # still asks for one; "..., without converting to a percentage" does not)
    for clause in re.split(r"[,;:.!?()]", question):
        if _ASKS_PERCENT.search(clause) and _ANY_NEGATION.search(clause):
            return False
    return True


def _gold_percent_columns(gold: ResultSet, question: str) -> list[int]:
    if not asks_percent(question):
        return []
    points = bool(_ASKS_POINTS.search(question))
    return [
        j
        for j, name in enumerate(gold.columns)
        if (PERCENT_WORDS & words(name) or (points and words(name) >= POINT_CHANGE_WORDS))
        and not _percentile_name(name)
    ]


_PERCENTILE_WORDS = frozenset(
    {"percentile", "pctile", "quantile", "median", "p50", "p90", "p95", "p99"}
)


def _percentile_name(name: str) -> bool:
    """A percentile, not a percentage: `latency_pct95`, `p95`, `pctile`, or pct beside a number."""
    ws = words(name)
    # a two-digit suffix is a percentile (pct95, p99); a four-digit one is a year (pct2023)
    suffix = re.search(r"(?i)(?:^|[^a-z])p(?:ct)?\d{2}(?!\d)", name)
    return bool(_PERCENTILE_WORDS & ws) or bool(suffix)


def percent_columns(
    predicted: ResultSet, gold: ResultSet, question: str, rel_tol: float, abs_tol: float
) -> Converted:
    """{predicted column: (its values x100, the gold percent columns they fit)} for each
    fraction column that fits a gold percent column only once scaled, under the percent-unit
    tolerance; {} when the rule does not apply."""
    targets = [(j, _Column(_column(gold, j))) for j in _gold_percent_columns(gold, question)]
    if not targets:
        return {}
    tol = max(abs_tol, PERCENT_ABS)
    out: Converted = {}
    for i in range(len(predicted.columns)):
        values = _column(predicted, i)
        if not any(_is_num(v) for v in values):
            continue
        scaled = [v * PERCENT_SCALE if _is_num(v) else v for v in values]
        before, after = _Column(values), _Column(scaled)
        fit = frozenset(
            j
            for j, g in targets
            if after.fits(g, rel_tol, tol) and not before.fits(g, rel_tol, tol)
        )
        if fit:
            out[i] = (scaled, fit)
    return out


# 2. pivot


def _label_key(text: str) -> str:
    return _SEPARATORS.sub("_", text.strip().casefold())


def _char_kinds(text: str) -> frozenset[str]:
    return frozenset(
        "letter" if c.isalpha() else "digit" if c.isdigit() else "sep" if c in " _-" else "other"
        for c in text.strip()
    )


def _omitted_label(name: str, labels: Collection[str], gold: ResultSet) -> bool:
    """A predicted column name that could be a label the gold's GROUP BY left out."""
    if SUMMARY_WORDS & any_words(name) or _has_summary_word(name):
        return False
    if _label_key(name) in {_label_key(c) for c in gold.columns}:
        return False
    # beside labels that start with a digit (years, dates, "2023 Q1") an omitted label would
    # too: ``unknown`` is not one (agentic-analytics 5a203a7)
    if any(_label_key(lab)[:1].isdigit() for lab in labels) and not _label_key(name)[:1].isdigit():
        return False
    kinds = frozenset().union(*(_char_kinds(lab) for lab in labels))
    return bool(name.strip()) and _char_kinds(name) <= kinds


def pivot(predicted: ResultSet, gold: ResultSet) -> ResultSet | None:
    """The one wide predicted row unpivoted into the gold's long shape, or None."""
    if len(gold.columns) != 2 or not gold.rows or len(predicted.rows) != 1:
        return None
    text = [all(isinstance(r[j], str) for r in gold.rows) for j in (0, 1)]
    nums = [all(_is_num(r[j]) for r in gold.rows) for j in (0, 1)]
    if text[0] and nums[1]:
        label_col = 0
    elif text[1] and nums[0]:
        label_col = 1
    else:
        return None
    labels: dict[str, str] = {}
    for r in gold.rows:
        labels.setdefault(_label_key(r[label_col]), r[label_col])
    if len(labels) != len(gold.rows):
        return None
    keys = [_label_key(c) for c in predicted.columns]
    if len(set(keys)) != len(keys) or not set(labels) <= set(keys):
        return None
    (row,) = predicted.rows
    long: list[tuple[Any, Any]] = []
    for name, key, cell in zip(predicted.columns, keys, row, strict=True):
        if key in labels:
            pair = (labels[key], cell) if label_col == 0 else (cell, labels[key])
            long.append(pair)
        elif not (_is_num(cell) and cell == 0 and _omitted_label(name, labels.values(), gold)):
            return None  # an extra column must be a label the gold omitted, so zero
    return ResultSet(columns=gold.columns, rows=tuple(long))


# 3. empty_check


def _numeric(cell: Any) -> float | None:
    if _is_num(cell):
        return float(cell)
    if isinstance(cell, str) and _PLAIN_NUMBER.fullmatch(cell):
        return float(cell)
    return None


def _leading_words(name: str) -> frozenset[str]:
    """A name's first word, read whole and at a case change (``totalSales`` -> totalsales,
    total)."""
    firsts = (
        _WORD.findall(name.casefold())[:1],
        _WORD.findall(_CAMEL.sub(" ", name).casefold())[:1],
    )
    return frozenset(w for first in firsts for w in first)


def is_count_column(name: str) -> bool:
    """A column named as a count (``n``, ``sales_in_1994``), not as another measure, and not
    a column that *is* a period, an id, a rank or a total (its leading word: ``total_sales``,
    ``year_count``)."""
    if not words(name) & COUNT_WORDS or any_words(name) & NOT_COUNT_WORDS:
        return False
    if _has_summary_word(name):  # sales_total, overall_total_sales, all_sales, grandtotal
        return False
    return not _leading_words(name) & NOT_A_COUNT_LEADS


def _has_summary_word(name: str) -> bool:
    """A total, sum or overall figure anywhere in the name, as a word or run together
    (``grandtotal``), after NFKC (fullwidth letters); a zero total is not a no-data check."""
    folded = unicodedata.normalize("NFKC", name).casefold()
    return bool(TOTAL_WORDS & any_words(folded)) or any(
        w in folded for w in ("total", "grand", "overall")
    )


TOTAL_WORDS = frozenset({"total", "totals", "subtotal", "sum", "all", "overall", "grand"})


def empty_check(predicted: ResultSet, gold: ResultSet) -> ResultSet | None:
    """No rows, in the gold's columns, when the one predicted row is a no-data check."""
    if gold.rows or len(predicted.rows) != 1:
        return None
    (row,) = predicted.rows
    counts = 0
    for name, cell in zip(predicted.columns, row, strict=True):
        n = _numeric(cell)
        if n is not None:
            if n != 0 or not is_count_column(name):
                return None
            counts += 1
        elif not (cell is None or (isinstance(cell, str) and _ISO_DATE.fullmatch(cell.strip()))):
            return None  # text, booleans and nested values are not a no-data check
    return ResultSet(columns=gold.columns, rows=()) if counts else None


# 4. period_number


_QUARTER_LABELS = (  # (pattern, year group, quarter group); a bare "Q1" states no year
    (re.compile(r"(\d{4})[\s-]*Q([1-4])", re.ASCII | re.IGNORECASE), 1, 2),
    (re.compile(r"Q([1-4])[\s-]*(\d{4})", re.ASCII | re.IGNORECASE), 2, 1),
    (re.compile(r"Q([1-4])", re.ASCII | re.IGNORECASE), 0, 1),
)
_MONTH_NAMES = {
    name: month
    for month, names in enumerate(
        (
            ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"),
            ("may",), ("june", "jun"), ("july", "jul"), ("august", "aug"),
            ("september", "sep", "sept"), ("october", "oct"), ("november", "nov"),
            ("december", "dec"),
        ),
        start=1,
    )
    for name in names
}  # fmt: skip
_MONTH_NUMBER_LABEL = re.compile(r"(\d{4})-(\d{2})", re.ASCII)
_MONTH_NAME_LABELS = (  # (pattern, year group, month-name group)
    (re.compile(r"([A-Za-z]+)\.?\s+(\d{4})", re.ASCII), 2, 1),
    (re.compile(r"(\d{4})\s+([A-Za-z]+)", re.ASCII), 1, 2),
)
# fiscal periods are not calendar months or quarters ("Q1 of FY2024" may start in April)
_FISCAL = re.compile(r"\bFY\s?'?\d{2,4}\b|\bfiscal\b", re.IGNORECASE)

Period = tuple[int | None, int, bool]  # (stated year or None, first month, a quarter label)


def _period_cell(v: Any) -> Period | None:
    """One cell as a period start: a month-start date, a quarter label (``2023 Q1``,
    ``Q1 2023``, ``2023-Q1``, ``Q1``) or a month label (``2023-03``, ``March 2023``,
    ``Mar 2023``); None for anything else (``Q5``, ``2023-13``, ``2023-W05``)."""
    if not isinstance(v, str):
        return None
    text = v.strip()
    if (m := _PERIOD_START.fullmatch(text) or _MONTH_NUMBER_LABEL.fullmatch(text)) is not None:
        month = int(m.group(2))
        return (int(m.group(1)), month, False) if 1 <= month <= 12 else None
    for pattern, year_at, quarter_at in _QUARTER_LABELS:
        if (m := pattern.fullmatch(text)) is not None:
            year = int(m.group(year_at)) if year_at else None
            return year, 3 * int(m.group(quarter_at)) - 2, True
    for pattern, year_at, name_at in _MONTH_NAME_LABELS:
        m = pattern.fullmatch(text)
        if m is not None and (month := _MONTH_NAMES.get(m.group(name_at).casefold())):
            return int(m.group(year_at)), month, False
    return None


def _period_starts(values: Sequence[Any]) -> list[Period | None] | None:
    """A period per cell when every non-null cell is a period start, else None."""
    out: list[Period | None] = []
    for v in values:
        if v is None:
            out.append(None)
            continue
        cell = _period_cell(v)
        if cell is None:
            return None
        out.append(cell)
    return out if any(p is not None for p in out) else None


def _period_numbers(gold: ResultSet, named: frozenset[str], top: int) -> frozenset[int]:
    """The gold columns named as the period whose every cell is an integer in 1..top."""
    out = []
    for j, name in enumerate(gold.columns):
        cells = _column(gold, j)
        if not named & words(name) or not cells:
            continue
        if NOT_A_PERIOD_WORDS & words(name):  # week_of_month, day_of_month, month_count
            continue
        if all(_is_num(v) and float(v).is_integer() and 1 <= v <= top for v in cells):
            out.append(j)
    return frozenset(out)


def period_columns(predicted: ResultSet, gold: ResultSet, question: str) -> list[Converted]:
    """The month-number and quarter-number readings of the predicted period columns that
    apply, each converted column bound to the gold columns named as that period."""
    if not gold.rows or _FISCAL.search(question):
        return []
    years = {int(y) for y in _NAMES_YEAR.findall(question)}
    dates: dict[int, list[Period | None]] = {}
    for i in range(len(predicted.columns)):
        starts = _period_starts(_column(predicted, i))
        if starts is None:
            continue
        if any(p is not None and p[0] is None for p in starts):
            if len(years) != 1:
                continue  # a bare "Q1" is read only in the one year the question names
            (year,) = years
            starts = [p if p is None or p[0] is not None else (year, p[1], p[2]) for p in starts]
        in_years = {p[0] for p in starts if p is not None}
        if len(in_years) == 1 and (not years or in_years <= years):
            dates[i] = starts
    if not dates:
        return []
    out: list[Converted] = []
    months = _period_numbers(gold, MONTH_WORDS, 12)
    quarter_labels = any(p is not None and p[2] for s in dates.values() for p in s)
    if _NAMES_MONTHS.search(question) and months and not quarter_labels:
        out.append(
            {i: ([None if p is None else p[1] for p in s], months) for i, s in dates.items()}
        )
    quarters = _period_numbers(gold, QUARTER_WORDS, 4)
    quarter_starts = all(p is None or p[1] in QUARTER_STARTS for s in dates.values() for p in s)
    if quarter_starts and _NAMES_QUARTERS.search(question) and quarters:
        out.append(
            {
                i: ([None if p is None else (p[1] - 1) // 3 + 1 for p in s], quarters)
                for i, s in dates.items()
            }
        )
    return out
