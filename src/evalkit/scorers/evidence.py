"""Structured evidence (SEV-01): judge a decision's reasons against the tabular record it was
made on.

Two parts, built for fraud-review-bedrock (Tech Spec §5.5 and EVK-02, MIT licence,
Copyright (c) 2026 Biplob Chakraborty, the same author) and general to any decision system:

- `render_evidence` turns a record into numbered evidence sentences, so `faithfulness()` can
  judge each reason against the record exactly as it judges an explanation against a document
  (sentence i of the list is evidence sentence i; the judge prompt numbers them).
- `value_check` checks the numbers and comparisons in a reason against the record without a
  model: phrases a project declares ("9x usual spend" -> `amt_vs_usual`, "at night" ->
  `hour < 5`) and every other number, within the displayed precision or a relative tolerance;
  a comparison against the field its clause names ("risk score is below 0.9"); times of day
  against a named hour field; declared policy constants and rule ids are recorded, not
  checked.

Templates read plain field names only; conditions use the RUL-01 predicate grammar. Nothing
from a record, template, phrase or reason is ever executed.
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import model_validator

from evalkit.schemas import Case, Completion
from evalkit.schemas._base import Model, Name, NonBlank
from evalkit.scorers.base import Scorer, ScoreResult
from evalkit.scorers.rows import Number, extract_numbers
from evalkit.scorers.rules import (
    RuleError,
    _evaluate,
    _missing,
    _names,
    _type,
    decision_fields,
    parse_predicate,
    register_task_scorer,
)

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SPEC = re.compile(r"[<>=^]?[+\- ]?#?0?[0-9]{0,3}[,_]?(?:\.[0-9]{1,3})?[eEfFgG%d]?")

# ------------------------------------------------------------------ evidence sentences


def _scalar(value: Any, field: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float | str):
        return str(value)
    raise ValueError(f"field {field!r} holds a {type(value).__name__}; render scalars only")


def _placeholder(body: str, record: Mapping[str, Any], missing: list[str]) -> str:
    """One `{...}`: `name`, `name:spec` or `name?if true|if false` (the first `:` or `?`
    ends the name, so the texts of a condition may hold colons)."""
    name, sep, rest = body, "", ""
    marks = [i for i in (body.find(":"), body.find("?")) if i != -1]
    if marks:
        cut = min(marks)
        name, sep, rest = body[:cut], body[cut], body[cut + 1 :]
    if not _NAME.fullmatch(name):
        raise ValueError(f"template placeholder {{{body}}} must start with a plain field name")
    if name not in record:
        raise ValueError(f"template reads field {name!r}, which the record does not have")
    value = record[name]
    if _missing(value):
        missing.append(name)
        return ""
    if sep == ":":
        if not _SPEC.fullmatch(rest) or isinstance(value, bool):
            raise ValueError(f"template placeholder {{{body}}}: a number format spec only")
        if not isinstance(value, int | float):
            raise ValueError(f"template placeholder {{{body}}}: {name!r} is not a number")
        return format(value, rest)
    if sep == "?":
        yes, bar, no = rest.partition("|")
        if not bar or "|" in no:
            raise ValueError(f"template placeholder {{{body}}}: write {{{name}?if true|if false}}")
        if not isinstance(value, bool):
            raise ValueError(f"template placeholder {{{body}}}: {name!r} is not true/false")
        return yes if value else no
    return _scalar(value, name)


def _render(template: str, record: Mapping[str, Any]) -> str:
    out: list[str] = []
    missing: list[str] = []
    i = 0
    while i < len(template):
        ch = template[i]
        if ch in "{}" and template[i : i + 2] in ("{{", "}}"):
            out.append(ch)
            i += 2
        elif ch == "{":
            end = template.find("}", i + 1)
            body = template[i + 1 : end] if end != -1 else ""
            if end == -1 or "{" in body:
                raise ValueError(f"template {template!r}: unclosed or nested placeholder")
            out.append(_placeholder(body, record, missing))
            i = end + 1
        elif ch == "}":
            raise ValueError(f"template {template!r}: a single '}}' (write '}}}}')")
        else:
            out.append(ch)
            i += 1
    if missing:
        names = list(dict.fromkeys(missing))
        return f"{' and '.join(names)} {'is' if len(names) == 1 else 'are'} not known."
    sentence = "".join(out).strip()
    if not sentence:
        raise ValueError(f"template {template!r} renders an empty sentence")
    return sentence


def render_evidence(record: Mapping[str, Any], template: Sequence[str] | None = None) -> list[str]:
    """The record as evidence sentences, one per template line, in order (sentence i is
    evidence sentence i for `FaithfulnessItem.evidence`). Placeholders: `{field}`,
    `{field:spec}` (number format, e.g. `.1f`), `{field?if true|if false}`; `{{` and `}}` are
    braces. A None or non-finite value makes its sentence `<field> is not known.`; a field the
    record lacks is a ValueError. With no template: `<field> is <value>.` per field."""
    if isinstance(template, str):
        raise ValueError("template is a list of sentences, not one string")
    if template is None:
        return [
            f"{k} is not known." if _missing(v) else f"{k} is {_scalar(v, k)}."
            for k, v in record.items()
        ]
    return [_render(t, record) for t in template]


# ------------------------------------------------------------------ value check


class ValuePhrase(Model):
    """A phrase in a reason and what it claims: a field's value (`field`) or a condition on
    the record (`holds`, a predicate in the rule grammar). `pattern` is a regex, case-folded."""

    pattern: NonBlank
    field: Name | None = None
    holds: NonBlank | None = None

    @model_validator(mode="after")
    def _check(self) -> ValuePhrase:
        if (self.field is None) == (self.holds is None):
            raise ValueError("a value phrase names exactly one of field or holds")
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(f"pattern {self.pattern!r} is not a regex: {exc}") from exc
        if self.holds is not None:
            parse_predicate(self.holds, None, f"phrase {self.pattern!r}")
        return self


# A comparison word right before the number, optionally negated ("not above", "never over",
# "doesn't exceed", "no more than"): a negation inverts the comparison (B5.13 verifier pass 3).
_CMP_WORDS = (
    r"less than or equal to|greater than or equal to|more than or equal to|"
    r"at or below|at or above|more than|greater than|higher than|fewer than|less than|"
    r"lower than|at least|at most|up to|exceeding|exceeds|exceed|above|below|under|over|"
    r"meets|meet|reaches|reach|"
    r">=|<=|≥|≤|>|<"
)


def _comparator(words: str) -> re.Pattern[str]:
    # a negation DIRECTLY before the comparison ("not above", "no more than", "never over",
    # "cannot exceed", "doesn't exceed") inverts it; any other negation earlier in the same
    # sentence makes the comparison ambiguous (see `_comparison`). "below the 5x threshold"
    # reads as "below 5x".
    return re.compile(
        r"(?P<neg>(?:\b(?:not|no|never|cannot)|n['’‘]t)\s+)?"
        rf"(?P<cmp>{words})\s*(?:\bboth\s+)?(?:\bthe\s+)?[$£€]?\s*$",
        re.IGNORECASE,
    )


_COMPARATOR = _comparator(_CMP_WORDS)
# a time of day may also be "after 10 p.m." or "before 6 a.m."
_TIME_COMPARATOR = _comparator(_CMP_WORDS + r"|after|before|later than|earlier than")
# "...$800 or less", "41 or more transactions": a comparison stated after the number
_POSTFIX = re.compile(
    r"^\s*(?:[a-z%]+\s+){0,2}?or\s+(?P<word>less|fewer|lower|below|under|more|greater|higher|"
    r"above|over)\b",
    re.IGNORECASE,
)
_COMPARATOR_WINDOW = 40  # characters before a number searched for a comparison word
_OP = {
    "less than or equal to": "<=",
    "at or below": "<=",
    "at most": "<=",
    "up to": "<=",
    "<=": "<=",
    "≤": "<=",
    "greater than or equal to": ">=",
    "more than or equal to": ">=",
    "at or above": ">=",
    "at least": ">=",
    "meets": ">=",  # "meets the 5x signal"
    "meet": ">=",
    "reaches": ">=",
    "reach": ">=",
    ">=": ">=",
    "≥": ">=",
    "more than": ">",
    "greater than": ">",
    "higher than": ">",
    "exceeding": ">",
    "exceeds": ">",
    "exceed": ">",
    "above": ">",
    "over": ">",
    ">": ">",
    "fewer than": "<",
    "less than": "<",
    "lower than": "<",
    "below": "<",
    "under": "<",
    "<": "<",
    "after": ">=",  # times only: "after 10 p.m." is hour >= 22
    "later than": ">=",
    "before": "<",
    "earlier than": "<",
}
_NEGATE = {">": "<=", ">=": "<", "<": ">=", "<=": ">"}
# Any negation word. One in the same sentence that is not directly before the comparison word
# ("no doubt above $900", "not only above", "without exceeding", "not no more than") makes the
# comparison ambiguous: the check then fails rather than guess (B5.13 verifier pass 5). False
# claims never pass because of a negation; some true ones fail (documented limit).
_ANY_NEGATION = re.compile(
    r"(?:\b(?:not|no|non|never|nothing|none|nor|neither|without|cannot|no-one|nobody)\b"
    r"|n['’‘]t\b)",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?]\s+(?=[A-Z])")  # a new sentence starts with a capital
AMBIGUOUS = "?"
_POSTFIX_OP = {w: "<=" for w in ("less", "fewer", "lower", "below", "under")} | {
    w: ">=" for w in ("more", "greater", "higher", "above", "over")
}


def _close(n: Number, v: float, tolerance: float) -> bool:
    """`n` equals v to its displayed precision or within the relative tolerance; a percent
    may also equal v as a fraction. A number written with a minus needs a negative v; an
    unsigned one is compared with |v|."""
    if n.negative and v >= 0:
        return False
    v = abs(v)
    for x, half, kind in n.candidates:
        for xx, hh in ((x, half), (x / 100, half / 100)) if kind == "pct" else ((x, half),):
            eps = 1e-9 * max(1.0, xx)
            if abs(xx - v) <= max(hh, tolerance * max(xx, v)) + eps:
                return True
    return False


def _claim(n: Number, op: str, v: float, tolerance: float) -> bool:
    if op == AMBIGUOUS:
        return False
    if op == "≈":
        return _close(n, v, tolerance)
    x = n.value
    # a percent compared with a field held as a fraction (|v| <= 1, e.g. risk_score 0.83) is
    # read as a fraction: "over 80%" is 0.80, "at most 1%" is 0.01 (B5.13 verifier pass 2)
    if n.candidates[0][2] == "pct" and abs(v) <= 1:
        x = x / 100
    # a written minus compares signed values; an unsigned number compares with |v|
    x, v = (-x, v) if n.negative else (x, abs(v))
    return {">": v > x, ">=": v >= x, "<": v < x, "<=": v <= x}[op]


def _as_float(v: Any) -> float | None:
    """A finite float for a numeric record value, else None (huge ints, NaN, other types)."""
    if _missing(v) or _type(v) != "number":
        return None
    try:
        f = float(v)
    except (OverflowError, TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _numeric(record: Mapping[str, Any], name: str, where: str) -> float | None:
    """The field's value; None when the record lacks it or it is missing."""
    value: Any = record.get(name)
    if value is None or _missing(value):
        return None
    if _type(value) != "number":
        raise RuleError(f"{where}: record field {name!r} is not a number")
    f = _as_float(value)
    if f is None:
        raise RuleError(f"{where}: record field {name!r} is not a finite number")
    return f


def _condition(
    holds: str, record: Mapping[str, Any], where: str
) -> tuple[bool, dict[str, Any], str | None]:
    """(holds, the values read, `missing_value:<field>` when a field is absent or missing)."""
    tree = parse_predicate(holds, None, where)
    names = _names(tree)
    found = {n: record.get(n) for n in names}
    absent = [n for n, v in found.items() if _missing(v)]
    if absent:
        return (
            False,
            {n: (None if _missing(v) else v) for n, v in found.items()},
            (f"missing_value:{absent[0]}"),
        )
    types: dict[str, Any] = {}
    for name, value in found.items():
        kind = _type(value)
        if kind == "other":
            raise RuleError(f"{where}: record field {name!r} is not a number, bool or text")
        types[name] = kind
    typed = parse_predicate(holds, types, where)
    return bool(_evaluate(typed.body, record, strict=True)), found, None


def _finite(n: Number) -> bool:
    return all(math.isfinite(x) and math.isfinite(h) for x, h, _ in n.candidates)


# ---- reading the reason: times, ids, constants, field names, sentences and clauses

# "10 p.m.", "6am", "10:30 pm", "11 P.M." (hour 1-12, or 0 with am); "22:00" (24-hour clock)
_MERIDIEM_TIME = re.compile(
    r"(?<![\w.,:])(?P<h>\d{1,2})(?:[:.](?P<m>[0-5]\d))?\s?(?P<ap>[ap])\.?\s?m\b\.?",
    re.IGNORECASE,
)
_CLOCK_TIME = re.compile(r"(?<![\w.,:])(?P<h>[01]?\d|2[0-3]):(?P<m>[0-5]\d)(?![\d:]|[.,]\d)")
_WORD_TIME = re.compile(r"\b(?:midnight|noon|midday)\b", re.IGNORECASE)
# two times joined into a window: "10 p.m. to 6 a.m.", "22:00-06:00", "between 10pm and 6am"
_RANGE_JOIN = re.compile(
    r"\s*[-–—]\s*|\s*(?:[-–—]\s*)?(?:to|until|till|through|and)(?:\s*[-–—])?\s*", re.IGNORECASE
)
# "10 to 6 a.m.": a bare hour before a joined time is that window's first end
_BARE_HOUR = re.compile(r"(?<![\w.,:])(?:1[0-2]|0?\d)\s*(?:-|–|—|to|until|till|through)\s*$")
# a clause or rule id: "rule 7.1", "clause 5.9", "section 3", "§4.2" (ids like "P7.1" are never
# read as numbers at all: a number glued to a letter is not a number claim)
_RULE_ID = re.compile(
    r"(?:\b(?:rules?|clauses?|sections?|articles?|paragraphs?)\s+|§\s*)(?:[A-Z]{1,3}[\s-]?)?"
    r"(?P<id>\d+(?:\.\d+)*)\b",
    re.IGNORECASE,
)
# a word that marks a number as a policy threshold when it sits next to it ("the 5x signal",
# "threshold of 0.9", "the 10x never-auto-clear threshold"): see `constants` in `value_check`
_CUE = re.compile(
    r"\b(?:thresholds?|limits?|cut-?offs?|signals?|levels?|rules?|clauses?|polic(?:y|ies)|"
    r"triggers?|bars?|lines?|caps?|floors?|ceilings?|minimums?|maximums?|bands?|boundar"
    r"(?:y|ies)|criteri(?:on|a)|windows?|cutoffs?)\b",
    re.IGNORECASE,
)
_CUE_WINDOW = 3  # words between a number and its cue word
_WORD_GAP = r"(?:[\s,()\-]*[\w'’\-]+){0,%d}?[\s,()\-]*"
_CUE_BEFORE = re.compile(rf"{_CUE.pattern}{_WORD_GAP % _CUE_WINDOW}$", re.IGNORECASE)
_CUE_AFTER = re.compile(rf"^(?:[a-z%×]*){_WORD_GAP % _CUE_WINDOW}{_CUE.pattern}", re.IGNORECASE)
# Clause ends for naming fields: a sentence end, ";", a new line, or a contrast (", but",
# ", while", ", whereas", ", yet", ", though", ", although"). A field named in another clause
# is not the field a number talks about; ", and" / ", so" usually continue the same subject
# ("risk score is 0.39, and well below 0.9").
_CLAUSE_END = re.compile(
    r"[.!?]\s+(?=[A-Z])|[;\n]|,\s+(?i:but|while|whereas|yet|though|although)\b"
)
_HOUR_TOKENS = frozenset({"hour", "hours", "hr", "hrs"})
# A multiple ("5x", "5 times", "10 or more times") is a ratio: it names only a field whose key
# marks a ratio ("amt_vs_usual", "spend_ratio"), never a plain amount named nearby.
_RATIO_TOKENS = frozenset(
    {"vs", "versus", "ratio", "multiple", "multiplier", "times", "x", "fold", "rel", "relative"}
)
_TIMES_AFTER = re.compile(
    r"\s*(?:or\s+(?:more|less|fewer|greater|higher|lower|above|below|over|under)\s+)?"
    r"(?:times\b|×|-?fold\b)",
    re.IGNORECASE,
)
_NEAREST = 16  # mentions read on each side of a number when naming its field
_MIN_NAME = 3  # shortest field name read from a record key (aliases may be shorter)
# a negation directly before a condition phrase ("not a first purchase", "never at night")
_HOLDS_NEGATION = re.compile(
    r"(?:\b(?:not|no|never|cannot)|n['’‘]t)\s+(?:(?:a|an|the|at|in|on|by|for|during)\s+)?$",
    re.IGNORECASE,
)
_WINDOW_BEFORE = 40  # characters before a condition phrase searched for its own negation


def _tokens(key: str) -> list[str]:
    return [t for t in re.split(r"[_\W]+", key.lower()) if t]


def _name_pattern(name: str, *, alias: bool = False) -> re.Pattern[str] | None:
    """A record key as words ("risk_score" matches "risk score", "Risk-Score", "risk_score"),
    or an alias as written (any run of spaces matches any run of spaces); an optional plural
    or possessive s; case-insensitive."""
    words = name.split() if alias else _tokens(name)
    if not words:
        return None
    body = (r"\s+" if alias else r"[\s_\-]+").join(re.escape(w) for w in words)
    return re.compile(rf"(?<![A-Za-z0-9_]){body}(?:s|['’]s)?(?![A-Za-z0-9_])", re.IGNORECASE)


@dataclass(frozen=True)
class _Mention:
    start: int
    end: int
    field: str


@dataclass
class _Text:
    """One reason, read once: sentence starts, clause ends, negations and field mentions."""

    text: str
    sentences: list[int]  # start of each sentence
    clause_ends: list[tuple[int, int]]  # (start, end) of each clause end, in order
    negations: list[int]  # start of each negation word
    mentions: list[_Mention]  # in order, not overlapping

    def __post_init__(self) -> None:
        self._end_starts = [s for s, _ in self.clause_ends]
        self._end_ends = [e for _, e in self.clause_ends]
        self._mention_starts = [m.start for m in self.mentions]

    def sentence_start(self, pos: int) -> int:
        return self.sentences[bisect.bisect_right(self.sentences, pos) - 1]

    def negated_between(self, start: int, end: int) -> bool:
        i = bisect.bisect_left(self.negations, start)
        return i < len(self.negations) and self.negations[i] < end

    def clause(self, start: int, end: int) -> tuple[int, int]:
        """The clause holding the span [start, end)."""
        i = bisect.bisect_right(self._end_ends, start) - 1
        lo = self._end_ends[i] if i >= 0 else 0
        j = bisect.bisect_left(self._end_starts, end)
        hi = self._end_starts[j] if j < len(self._end_starts) else len(self.text)
        return lo, hi

    def named(self, start: int, end: int, fields: Mapping[str, Any]) -> list[str]:
        """Fields from `fields` named in the clause of [start, end): the nearest mention before
        the span first, then the following ones, nearest first (each field once; the nearest
        `_NEAREST` mentions each way are read)."""
        lo, hi = self.clause(start, end)
        mid = bisect.bisect_left(self._mention_starts, start)
        before: list[str] = []
        for m in reversed(self.mentions[max(0, mid - _NEAREST) : mid]):
            if m.start < lo:
                break
            if m.end <= start and m.field in fields:
                before.append(m.field)
        after: list[str] = []
        for m in self.mentions[mid : mid + _NEAREST]:
            if m.start >= hi:
                break
            if m.start >= end and m.end <= hi and m.field in fields:
                after.append(m.field)
        return list(dict.fromkeys([*before, *after]))


def _mentions(
    text: str, record: Mapping[str, Any], aliases: Mapping[str, Sequence[str]]
) -> list[_Mention]:
    """Where the reason names a record field: the key as words ("risk score"), its first word
    when that is distinctive ("risk" for risk_score), and the caller's aliases."""
    keys = list(dict.fromkeys([*(k for k in record if isinstance(k, str)), *aliases]))
    heads: dict[str, list[str]] = {}
    for k in keys:
        toks = _tokens(k)
        if len(toks) >= 2 and len(toks[0]) >= 4:
            heads.setdefault(toks[0], []).append(k)
    every_token = {k: set(_tokens(k)) for k in keys}
    found: list[_Mention] = []
    for k in keys:
        names = {k} if len(k) >= _MIN_NAME else set()
        toks = _tokens(k)
        head = toks[0] if toks else ""
        if (
            heads.get(head) == [k]
            and head not in record
            and not any(head in every_token[o] for o in keys if o != k)
        ):
            names.add(head)
        pats = [_name_pattern(x) for x in names]
        pats += [_name_pattern(x, alias=True) for x in aliases.get(k, ())]
        for pat in pats:
            if pat is not None:
                found += [_Mention(m.start(), m.end(), k) for m in pat.finditer(text)]
    # overlapping mentions of one field join ("card's usual" + "usual amount"); then the
    # longest mention wins where two fields overlap ("usual amount" over "amount")
    joined: list[_Mention] = []
    for m in sorted(found, key=lambda m: (m.field, m.start)):
        last = joined[-1] if joined else None
        if last is not None and last.field == m.field and m.start < last.end:
            joined[-1] = _Mention(last.start, max(last.end, m.end), m.field)
        else:
            joined.append(m)
    kept: list[_Mention] = []
    starts: list[int] = []
    for m in sorted(joined, key=lambda m: (-(m.end - m.start), m.start)):
        i = bisect.bisect_left(starts, m.start)
        if (i > 0 and kept[i - 1].end > m.start) or (i < len(kept) and kept[i].start < m.end):
            continue
        kept.insert(i, m)
        starts.insert(i, m.start)
    return kept


def _read(text: str, record: Mapping[str, Any], aliases: Mapping[str, Sequence[str]]) -> _Text:
    return _Text(
        text=text,
        sentences=[0] + [m.end() for m in _SENTENCE_END.finditer(text)],
        clause_ends=[(m.start(), m.end()) for m in _CLAUSE_END.finditer(text)],
        negations=[m.start() for m in _ANY_NEGATION.finditer(text)],
        mentions=_mentions(text, record, aliases),
    )


@dataclass(frozen=True)
class _Time:
    start: int
    end: int
    hour: int
    minute: int
    word: bool = False  # "midnight", "noon": only ever the end of a window


def _times(text: str) -> tuple[list[_Time], list[tuple[int, int]]]:
    """(point times, windows): "11 p.m." is a time; "10 p.m. to 6 a.m." and "midnight to
    5 a.m." are windows."""
    found: list[_Time] = []
    taken = bytearray(len(text) + 1)
    for m in _MERIDIEM_TIME.finditer(text):
        h = int(m.group("h"))
        if h > 12:
            continue
        hour = h % 12 + (12 if m.group("ap").lower() == "p" else 0)
        found.append(_Time(m.start(), m.end(), hour, int(m.group("m") or 0)))
        taken[m.start() : m.end()] = b"\x01" * (m.end() - m.start())
    for m in _CLOCK_TIME.finditer(text):
        if not taken[m.start()]:
            found.append(_Time(m.start(), m.end(), int(m.group("h")), int(m.group("m"))))
    for m in _WORD_TIME.finditer(text):
        hour = 0 if m.group(0).lower() == "midnight" else 12
        found.append(_Time(m.start(), m.end(), hour, 0, word=True))
    found.sort(key=lambda t: t.start)
    windows: list[tuple[int, int]] = []
    in_window: set[int] = set()
    for i, t in enumerate(found):
        nxt = found[i + 1] if i + 1 < len(found) else None
        if nxt is not None and _RANGE_JOIN.fullmatch(text, t.end, nxt.start):
            windows.append((t.start, nxt.end))
            in_window.update((i, i + 1))
        elif (
            i not in in_window
            and not t.word
            and (bare := _BARE_HOUR.search(text, max(0, t.start - 20), t.start))
        ):
            windows.append((bare.start(), t.end))  # "10 to 6 a.m."
            in_window.add(i)
    points = [t for i, t in enumerate(found) if i not in in_window and not t.word]
    return points, windows


def _cover(length: int, spans: Sequence[tuple[int, int]]) -> bytearray:
    """1 at every position inside one of `spans`."""
    marks = bytearray(length + 1)
    for start, end in spans:
        marks[start:end] = b"\x01" * (end - start)
    return marks


def _cued(text: str, n_start: int, n_end: int, lo: int, hi: int) -> bool:
    """A threshold word within a few words of the number, in its clause."""
    before = text[max(lo, n_start - 60) : n_start]
    after = text[n_end : min(hi, n_end + 60)]
    return bool(_CUE_BEFORE.search(before) or _CUE_AFTER.search(after))


# ---- the checks


# "above the 5x signal (P3.1) and the 10x limit": a number joined by "and the" to a compared
# one of the same kind (both multiples, or neither), with no comparison word, field name or
# number between, shares its comparison
_COORDINATED = re.compile(
    r"(?:\s*(?:x|×|times|%))?(?:(?!\b(?:" + _CMP_WORDS.replace("|>=|<=|≥|≤|>|<", "") + r")\b)"
    r"[^.;:\d<>≤≥])"
    r"{0,60}?(?:\(P?[\d.]+\)[^.;:\d<>≤≥]{0,20})?,?\s+and\s+the\s+",
    re.IGNORECASE,
)
# "for 801.52, which is at least 200": a comparison right after a number refers back to it
_REFERS_BACK = re.compile(
    r"(?:\s*(?:x|×|times|%|dollars|usd))?[\s,()]*(?:which|that)\s+(?:is|was)\b[^.;:\d]{0,40}",
    re.IGNORECASE,
)


_Claim = tuple[str, str | None, int, bool]  # (comparison, field, end of claim, a multiple?)


def _previous(
    read: _Text,
    numbers: Sequence[Number],
    resolved: Mapping[int, _Claim],
    i: int,
) -> _Claim | None:
    """The claim of the number before number i, when it is in the same clause and no field
    is named between them."""
    prev = resolved.get(i - 1)
    if prev is None:
        return None
    n = numbers[i]
    if read.clause(prev[2], prev[2]) != read.clause(n.start, n.start):
        return None
    k = bisect.bisect_left(read._mention_starts, prev[2])
    if k < len(read.mentions) and read.mentions[k].start < n.start:
        return None
    return prev


def _constant(n: Number, constants: Sequence[float], read: _Text, end: int) -> bool:
    """A declared policy constant with a threshold word next to it."""
    if not constants or not any(_close(n, c, 0.0) for c in constants):
        return False
    lo, hi = read.clause(n.start, end)
    return _cued(read.text, n.start, end, lo, hi)


def _value_check(
    *,
    text: str,
    n: Number,
    op: str,
    name: str | None,
    record: Mapping[str, Any],
    tolerance: float,
    where: str,
    kind: str,
    via: str | None = None,
) -> dict[str, Any]:
    """A number checked as `op` against the field `name` (None: no field backs it)."""
    check: dict[str, Any] = {"text": text, "kind": kind, "field": name, "op": op}
    if via is not None:
        check["via"] = via
    check["claimed"] = _claimed(n)
    if name is None:
        return {**check, "found": None, "ok": False}
    try:
        value = _numeric(record, name, where)
    except RuleError:  # a field of the wrong type fails this claim; never raises
        return {**check, "found": None, "ok": False, "reason": f"type_error:{name}"}
    ok = value is not None and _finite(n) and _claim(n, op, value, tolerance)
    check.update(found=value, ok=ok)
    return {**check, "reason": f"missing_value:{name}"} if value is None else check


def _time_check(
    *, text: str, t: _Time, op: str, name: str, record: Mapping[str, Any], where: str
) -> dict[str, Any]:
    """A time of day checked against an hour field: equal when the field's whole hour is the
    time's hour; a comparison uses hour + minutes / 60."""
    claimed = t.hour + t.minute / 60
    check: dict[str, Any] = {"text": text, "kind": "time", "field": name, "op": op}
    check["claimed"] = claimed
    try:
        value = _numeric(record, name, where)
    except RuleError:
        return {**check, "found": None, "ok": False, "reason": f"type_error:{name}"}
    if value is None:
        return {**check, "found": None, "ok": False, "reason": f"missing_value:{name}"}
    if op == AMBIGUOUS:
        ok = False
    elif op == "≈":
        ok = math.floor(value) == t.hour
    else:
        ok = {">": value > claimed, ">=": value >= claimed, "<": value < claimed}.get(
            op, value <= claimed
        )
    return {**check, "found": value, "ok": ok}


def _holds_negation(read: _Text, start: int) -> str | None:
    """How a negation reads a condition phrase at `start`: "invert" when one sits directly
    before it ("not a first purchase"), AMBIGUOUS when another is earlier in the sentence,
    else None."""
    sentence = read.sentence_start(start)
    window = read.text[max(sentence, start - _WINDOW_BEFORE) : start]
    direct = _HOLDS_NEGATION.search(window)
    cut = start - len(window) + direct.start() if direct else start
    if read.negated_between(sentence, cut):
        return AMBIGUOUS
    return "invert" if direct else None


def _check_reason(
    reason: str,
    record: Mapping[str, Any],
    phrases: Sequence[ValuePhrase],
    tolerance: float,
    *,
    aliases: Mapping[str, Sequence[str]] | None = None,
    constants: Sequence[float] = (),
    ignore: Sequence[re.Pattern[str]] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(checks, unchecked) for one reason."""
    text = reason  # the text read; check texts quote `reason`
    read = _read(text, record, aliases or {})
    points, windows = _times(text)
    unchecked: list[dict[str, Any]] = [
        {"text": reason[s:e], "kind": "time_window"} for s, e in windows
    ]
    timed = _cover(len(text), [*windows, *((t.start, t.end) for t in points)])
    ids = _cover(len(text), [(m.start("id"), m.end("id")) for m in _RULE_ID.finditer(text)])
    ignored = _cover(
        len(text),
        [(m.start(), m.end()) for p in ignore for m in p.finditer(text) if m.end() > m.start()],
    )
    numbers: list[Number] = []
    for n in extract_numbers(text):
        if timed[n.start]:
            continue  # a time of day, not a free number
        if ids[n.start] or ignored[n.start]:
            unchecked.append({"text": n.text, "kind": "id" if ids[n.start] else "ignored"})
            continue
        numbers.append(n)
    # times join the numbers so a declared phrase can claim one ("at 3 a.m." -> hour)
    items: list[tuple[Number, _Time | None]] = [(n, None) for n in numbers]
    for t in points:
        text_t = reason[t.start : t.end].strip()
        value_t = t.hour + t.minute / 60
        num = Number(text_t, t.start, ((value_t, 0.0, "num"),), plain_int=False, year_like=False)
        items.append((num, t))
    items.sort(key=lambda x: x[0].start)
    numbers = [n for n, _ in items]
    time_of = {i: t for i, (_, t) in enumerate(items) if t is not None}
    starts = [n.start for n in numbers]
    used: set[int] = set()
    checks: list[dict[str, Any]] = []
    # number index -> its claim: what a following "and the 10x" or ", which is above 200"
    # refers back to
    resolved: dict[int, _Claim] = {}
    for ph in phrases:
        where = f"phrase {ph.pattern!r}"
        if ph.holds is not None:
            condition: tuple[bool, dict[str, Any], str | None] | None = None
            for m in re.finditer(ph.pattern, text, re.IGNORECASE):
                if m.end() == m.start():
                    continue
                if condition is None:  # the record does not change: evaluate once
                    try:
                        condition = _condition(ph.holds, record, where)
                    except (RuleError, OverflowError, TypeError, ValueError):
                        # the phrase's grammar was checked when the ValuePhrase was built, so an
                        # error here comes from the record's values: fail this claim, never raise
                        condition = (False, {}, f"type_error:{ph.holds}")
                ok, found, why = condition
                negation = _holds_negation(read, m.start())
                if why is None and negation == "invert":
                    ok = not ok
                elif negation == AMBIGUOUS:
                    ok, why = False, why or "ambiguous_negation"
                check = {
                    "text": reason[m.start() : m.end()],
                    "kind": "condition",
                    "holds": ph.holds,
                    "negated": negation is not None,
                    "found": found,
                    "ok": ok,
                }
                checks.append({**check, "reason": why} if why else check)
            continue
        assert ph.field is not None
        for m in re.finditer(ph.pattern, text, re.IGNORECASE):
            i = bisect.bisect_left(starts, m.start())
            while i < len(numbers) and starts[i] < m.end() and i in used:
                i += 1
            if not (i < len(numbers) and starts[i] < m.end()):
                continue  # the phrase states no number here
            used.add(i)
            n = numbers[i]
            span = reason[m.start() : m.end()]
            if i in time_of:
                t = time_of[i]
                op = _comparison(text, read, t.start, t.end, time=True)
                checks.append(
                    _time_check(text=span, t=t, op=op, name=ph.field, record=record, where=where)
                )
                continue
            op = _comparison(text, read, n.start, n.start + len(n.text))
            resolved[i] = (op, ph.field, m.end(), n.multiple or "times" in span.lower())
            checks.append(
                _value_check(
                    text=span,
                    n=n,
                    op=op,
                    name=ph.field,
                    record=record,
                    tolerance=tolerance,
                    where=where,
                    kind="value",
                )
            )
    numeric = {k: f for k, v in record.items() if (f := _as_float(v)) is not None}
    # fields a number may name: numbers, and missing values (a named missing value fails)
    nameable: dict[str, Any] = {
        k: v for k, v in record.items() if isinstance(k, str) and (_missing(v) or k in numeric)
    }
    nameable |= {k: None for k in aliases or {} if k not in record}  # named, so it fails
    nameable |= {k: v for k, v in record.items() if isinstance(k, str) and _type(v) == "number"}
    hours = {k: v for k, v in nameable.items() if _HOUR_TOKENS & set(_tokens(k))}
    ratios = {k: v for k, v in nameable.items() if _RATIO_TOKENS & set(_tokens(k))}
    for i, n in enumerate(numbers):
        if i in used:
            continue
        end = n.start + len(n.text)
        if i in time_of:
            t = time_of[i]
            named = read.named(t.start, t.end, hours)
            if not named:
                unchecked.append({"text": n.text, "kind": "time"})
                continue
            op = _comparison(text, read, t.start, t.end, time=True)
            checks.append(
                _time_check(
                    text=n.text, t=t, op=op, name=named[0], record=record, where="time of day"
                )
            )
            continue
        op = _comparison(text, read, n.start, end)
        multiple = n.multiple or _TIMES_AFTER.match(text, end) is not None
        fields = ratios if multiple else nameable
        prev = _previous(read, numbers, resolved, i)
        if (
            op == "≈"
            and prev is not None
            and prev[3] == multiple
            and _COORDINATED.fullmatch(text, prev[2], n.start)
        ):
            op = prev[0]  # "above the 5x signal and the 10x limit": above both
        named = read.named(n.start, end, fields)
        name: str | None = None
        via = "value"
        if op == "≈":
            # equal: the nearest named field that matches
            name = next(
                (k for k in named if k in numeric and _close(n, numeric[k], tolerance)), None
            )
        elif named:
            # a comparison names its field: compare with it even when far from the number
            name = named[0]
        if name is not None:
            via = "name"
        elif (
            op not in ("≈", AMBIGUOUS)
            and prev is not None
            and prev[1] in fields
            and _REFERS_BACK.fullmatch(text, prev[2], n.start)
        ):
            # "for 801.52, which is at least 200": the field the previous number claimed
            name, via = prev[1], "reference"
        elif op == "≈" and not named and _constant(n, constants, read, end):
            # a policy threshold stated as such ("the 5x signal"), not an alert fact; a
            # comparison ("below the 5x signal") is a claim about the alert and is checked
            unchecked.append({"text": n.text, "kind": "constant", "value": _claimed(n)})
            continue
        else:
            # no field named: the first field within tolerance (B5.13 v0.3 reading); a
            # multiple only among the ratio fields
            pool = {k: v for k, v in numeric.items() if k in ratios} if multiple else numeric
            name = next((k for k, v in pool.items() if _close(n, v, tolerance)), None)
        resolved[i] = (op, name, end, multiple)
        checks.append(
            _value_check(
                text=n.text,
                n=n,
                op=op,
                name=name,
                record=record,
                tolerance=tolerance,
                where="number",
                kind="number",
                via=via if name is not None else None,
            )
        )
    for c in checks:
        if c.get("op") == AMBIGUOUS and "reason" not in c:
            c["reason"] = "ambiguous_negation"
    return checks, unchecked


def _comparison(text: str, read: _Text, start: int, end: int, *, time: bool = False) -> str:
    """The comparison stated around the number at [start, end), else "≈" (equal): a word
    right before it ("above 900"), inverted by a negation directly before that word ("not
    above 900", "doesn't exceed 900"), or a phrase right after it ("900 or less"). Any other
    negation earlier in the same sentence, however far back, makes it AMBIGUOUS."""
    sentence = read.sentence_start(start)
    window = text[max(sentence, start - _COMPARATOR_WINDOW) : start]
    cmp = (_TIME_COMPARATOR if time else _COMPARATOR).search(window)
    if cmp:
        op = _OP[re.sub(r"\s+", " ", cmp.group("cmp").lower())]
        first = cmp.start("neg") if cmp.group("neg") else cmp.start("cmp")
        if read.negated_between(sentence, start - len(window) + first):
            return AMBIGUOUS  # a negation not directly before the comparison
        return _NEGATE[op] if cmp.group("neg") else op
    if read.negated_between(sentence, start):
        # a negation before a number with no recognised comparison ("not-above $800",
        # "not even close to above $800") is not read as an equality claim either
        return AMBIGUOUS
    after = text[end : end + _COMPARATOR_WINDOW]
    post = _POSTFIX.match(after)
    return _POSTFIX_OP[post.group("word").lower()] if post else "≈"


def _claimed(n: Number) -> float | None:
    if not _finite(n):
        return None
    return -n.value if n.negative else n.value


def _options(
    aliases: Mapping[str, Sequence[str]] | None,
    constants: Sequence[float],
    ignore: Sequence[str],
) -> tuple[dict[str, tuple[str, ...]], tuple[float, ...], tuple[re.Pattern[str], ...]]:
    """Validate the caller's options (a bad option is the caller's error: ValueError)."""
    fixed_aliases: dict[str, tuple[str, ...]] = {}
    for field, names in (aliases or {}).items():
        if not isinstance(field, str) or isinstance(names, str):
            raise ValueError("aliases maps a field name to a list of phrases")
        if not all(isinstance(a, str) and a.strip() for a in names):
            raise ValueError(f"aliases[{field!r}]: every phrase is non-blank text")
        fixed_aliases[field] = tuple(names)
    if isinstance(constants, str | bytes):
        raise ValueError("constants is a list of numbers")
    fixed_constants: list[float] = []
    for c in constants:
        if isinstance(c, bool) or not isinstance(c, int | float) or not math.isfinite(c):
            raise ValueError(f"constant {c!r} is not a finite number")
        fixed_constants.append(float(c))
    if isinstance(ignore, str):
        raise ValueError("ignore is a list of regexes, not one string")
    patterns: list[re.Pattern[str]] = []
    for p in ignore:
        try:
            patterns.append(re.compile(p, re.IGNORECASE))
        except (re.error, TypeError) as exc:
            raise ValueError(f"ignore pattern {p!r} is not a regex: {exc}") from exc
    return fixed_aliases, tuple(fixed_constants), tuple(patterns)


def value_check(
    reason: str,
    record: Mapping[str, Any],
    *,
    phrases: Sequence[ValuePhrase] = (),
    tolerance: float = 0.1,
    aliases: Mapping[str, Sequence[str]] | None = None,
    constants: Sequence[float] = (),
    ignore: Sequence[str] = (),
) -> ScoreResult:
    """Check every number and declared phrase in `reason` against `record`. 1.0 when nothing
    fails (also when nothing was checkable), else 0.0. Details: `checks`, `failed` (the text of
    each failed check), `n_checked`, and `unchecked` (times, windows, rule ids, constants and
    ignored spans that were read but not checked; they never fail).

    A number written with a minus must match a negative value; an unsigned number ("fell
    0.83") is compared with the value's magnitude. A comparison ("risk score is below 0.9",
    "amount over 200") is checked against the field its clause names: a record key read as
    words ("risk_score" is "risk score", "risk_score", or "risk" when that first word is
    distinctive) or one of `aliases` (field -> phrases), the nearest mention before the number
    first. A multiple ("5x", "5 times") names only a ratio field ("amt_vs_usual"). "...and the
    10x limit" after "above the 5x signal" shares its comparison; ", which is at least 200"
    refers to the number before it. A comparison naming no field is checked against the
    first field within tolerance, and fails when there is none. A plain number passes when a
    field equals it within the displayed precision or `tolerance`.

    Times of day ("10 p.m.", "6am", "22:00") are not free numbers: one is checked against an
    hour field (a key with the word "hour") named in its clause, else recorded as unchecked;
    a window ("10 p.m. to 6 a.m.") is recorded as unchecked. A number equal to one of
    `constants` with a threshold word next to it ("the 5x signal", "threshold of 0.9") is a
    policy constant, recorded as unchecked, unless it is compared or its clause names a field.
    Numbers after "rule", "clause" or "section" are ids; ids like "P7.1" are never numbers.
    Numbers inside spans matched by an `ignore` regex are recorded as unchecked (negations
    and names there are still read).

    A negation directly before a comparison or a condition phrase inverts it ("not above 900",
    "not a first purchase at this merchant"); any other negation earlier in the same sentence
    makes the check fail as `ambiguous_negation`. A field the record lacks, or a missing
    value, fails the claim that needs it, with `reason` `missing_value:<field>`."""
    if not 0 <= tolerance < 1:
        raise ValueError("tolerance must be in [0, 1)")
    opts = _options(aliases, constants, ignore)
    checks, unchecked = _check_reason(
        reason, record, phrases, tolerance, aliases=opts[0], constants=opts[1], ignore=opts[2]
    )
    return _result(checks, unchecked)


def _result(checks: list[dict[str, Any]], unchecked: list[dict[str, Any]]) -> ScoreResult:
    failed = [c["text"] for c in checks if not c["ok"]]
    return ScoreResult(
        value=0.0 if failed else 1.0,
        details={
            "checks": checks,
            "failed": failed,
            "n_checked": len(checks),
            "unchecked": unchecked,
        },
    )


def value_check_scorer(
    *,
    phrases: Sequence[ValuePhrase] = (),
    name: str = "evidence_values",
    record_key: str = "record",
    tolerance: float = 0.1,
    aliases: Mapping[str, Sequence[str]] | None = None,
    constants: Sequence[float] = (),
    ignore: Sequence[str] = (),
    version: str = "1",
) -> Scorer:
    """Register a task scorer that runs `value_check` on each record: the record from
    `case.meta[record_key]`, each string in the `reasons` list of a JSON-object answer (else
    the answer text). Each check and unchecked entry carries `reason_index`, the position of
    its reason. A failed call scores 0.0."""
    if not 0 <= tolerance < 1:
        raise ValueError("tolerance must be in [0, 1)")
    fixed = tuple(phrases)
    al, consts, patterns = _options(aliases, constants, ignore)

    def run(case: Case, completion: Completion, **_: Any) -> ScoreResult:
        if completion.error is not None or completion.answer is None:
            return ScoreResult(value=0.0, details={"reason": completion.error or "no_answer"})
        record = case.meta.get(record_key)
        if not isinstance(record, Mapping):
            raise RuleError(f"case {case.id!r} has no {record_key!r} object in its meta")
        reasons = decision_fields(completion).get("reasons")
        if not (isinstance(reasons, list) and all(isinstance(r, str) for r in reasons)):
            reasons = [completion.answer.answer]
        checks: list[dict[str, Any]] = []
        unchecked: list[dict[str, Any]] = []
        for k, reason in enumerate(reasons):
            got, skipped = _check_reason(
                reason, record, fixed, tolerance, aliases=al, constants=consts, ignore=patterns
            )
            # a check's own `reason` (missing_value, type_error, ambiguous_negation) is kept
            checks += [{**c, "reason_index": k} for c in got]
            unchecked += [{**c, "reason_index": k} for c in skipped]
        return _result(checks, unchecked)

    run.__doc__ = value_check_scorer.__doc__
    return register_task_scorer(run, name, version)
