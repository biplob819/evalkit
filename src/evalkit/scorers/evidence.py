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
  a comparison against every field its clause names ("risk score is below 0.9"); times of
  day and windows against a named hour field; declared policy constants and rule ids are
  recorded, not checked, and an unchecked time, window or constant leaves its reason
  unverifiable. Each reason is `verified`, `contradicted` or `unverifiable` (D-72), and
  `value_check_shares` gives each share across a run with its count and an exact interval.

Templates read plain field names only; conditions use the RUL-01 predicate grammar. Nothing
from a record, template, phrase or reason is ever executed.
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import model_validator

from evalkit.schemas import Case, Completion, Interval
from evalkit.schemas._base import Model, Name, NonBlank, NonNegInt
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
from evalkit.stats import clopper_pearson

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
# a bound named before its number: "exceeds the threshold of 0.9" (not "falls under the rule
# at 10x": a rule, clause or policy is not a bound)
_BOUND_WORDS = (
    r"\b(?:thresholds?|limits?|cut-?offs?|cutoffs?|signals?|levels?|triggers?|bars?|lines?|"
    r"caps?|floors?|ceilings?|minimums?|maximums?|bands?|boundar(?:y|ies))\b"
)
# words that mark a number as a policy threshold ("the 5x signal", "threshold of 0.9")
_CUE_WORDS = (
    r"\b(?:thresholds?|limits?|cut-?offs?|signals?|levels?|rules?|clauses?|polic(?:y|ies)|"
    r"triggers?|bars?|lines?|caps?|floors?|ceilings?|minimums?|maximums?|bands?|boundar"
    r"(?:y|ies)|criteri(?:on|a)|windows?|cutoffs?)\b"
)


def _comparator(words: str) -> re.Pattern[str]:
    # a negation DIRECTLY before the comparison ("not above", "no more than", "never over",
    # "cannot exceed", "doesn't exceed") inverts it; any other negation earlier in the same
    # sentence makes the comparison ambiguous (see `_comparison`). "below the 5x threshold"
    # reads as "below 5x"; so do "exceeds the threshold of 0.9", "below the extreme-spend
    # limit of 10" (up to three words, a threshold word, then "of"), "above USD 850" and
    # "above both 800 and 900" (B5.13 verifier pass 6).
    return re.compile(
        r"(?P<neg>(?:\b(?:not|no|never|cannot)|n['’‘]t)\s+)?"
        rf"(?P<cmp>{words})\s*(?P<both>\bboth\s+)?(?:\bthe\s+)?"
        rf"(?:(?:[\w'’.\-]+\s+){{0,3}}?{_BOUND_WORDS}\s*(?:\s(?:of|at)\s+|\())?"
        r"(?:(?:USD|EUR|GBP|US\$|dollars?|euros?|pounds?)\s*)?[$£€]?\s*$",
        re.IGNORECASE,
    )


_COMPARATOR = _comparator(_CMP_WORDS)
# a time of day may also be "after 10 p.m." or "before 6 a.m."
_TIME_COMPARATOR = _comparator(_CMP_WORDS + r"|after|before|later than|earlier than")
# A comparison word in the clause that is not directly before the number (nor a recognised
# shape: "the threshold of", a currency word, "both N and M") leaves the comparison unclear:
# the check is `ambiguous` (reason `unclear_comparison`), never an equality (verifier pass 6).
_LOOSE_WORDS = (
    r"\b(?:above|below|under|over|exceed\w*|more|less|fewer|greater|higher|lower|least|most|"
    r"beyond|surpass\w*|cross(?:es|ed|ing)?|outside|within|between|meets?|reach(?:es|ed)?|"
    r"up\s+to)\b|[<>≤≥]"
)
_LOOSE_COMPARISON = re.compile(_LOOSE_WORDS, re.IGNORECASE)
# for a time of day, "after", "before", "later" and "earlier" compare too
_LOOSE_TIME = re.compile(_LOOSE_WORDS + r"|\b(?:after|before|later|earlier)\b", re.IGNORECASE)
UNCLEAR = "~"  # the comparison stated around a number cannot be read
_UNSURE_REASON = {"?": "ambiguous_negation", UNCLEAR: "unclear_comparison"}
# "...$800 or less", "41 or more transactions": a comparison stated after the number
_POSTFIX = re.compile(
    r"^\s*(?:[a-z%]+\s+){0,2}?or\s+(?P<word>less|fewer|lower|below|under|more|greater|higher|"
    r"above|over)\b",
    re.IGNORECASE,
)
_COMPARATOR_WINDOW = 64  # characters before a number searched for a comparison word
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
# comparison ambiguous: the check is then `ambiguous` rather than guessed (B5.13 verifier pass
# 5), and its reason unverifiable, never verified (D-72). False claims never pass because of a
# negation; some true ones cannot be verified (documented limit).
_ANY_NEGATION = re.compile(
    r"(?:\b(?:not|no|non|never|nothing|none|nor|neither|without|cannot|no-one|nobody|hardly|"
    r"barely|scarcely|nope)\b|n['’‘]t\b)",
    re.IGNORECASE,
)
_SENTENCE_END = re.compile(r"[.!?]\s+(?=[A-Z])")  # a new sentence starts with a capital
AMBIGUOUS = "?"
_UNSURE = frozenset({AMBIGUOUS, UNCLEAR})
# A negation AFTER a claim in its sentence (B5.13 verifier pass 6): "Amount above $800 is not
# true", "... is not the case", "812.40, not 900", "at night? no". It makes the claim ambiguous
# when no clause boundary lies between them, when it follows a comma, dash or bracket
# directly, or when a word of truth follows it ("..., which is not true", "... I cannot
# confirm"). A denial word after the claim ("is untrue", "is wrong") does too, and so does a
# next sentence that is only a denial ("A new merchant? Not at all."). A negation in a later
# clause with its own subject ("..., which is not a spend signal") does not.
_BOUNDARY = re.compile(
    r"[,;:()\[\]—–]|\s-\s|\b(?:which|that|so|because|since|but|and|while|whereas|though|"
    r"although|yet|hence|thus|therefore|meaning|where|when|as|if|unless)\b",
    re.IGNORECASE,
)
_TRUTH_AFTER = re.compile(
    r"[\w'’‘]*(?:\W+[\w'’‘]+){0,3}?\W+(?:true|the case|correct|accurate|accurately|right|"
    r"valid|real|exact|exactly|confirm|confirms|confirmed|confirmable|verify|verified|"
    r"verifiable|supported|substantiated|proven|prove|proved|proof|backed|borne|reliable|"
    r"precise|factual|fact|really|quite|certain|sure|happen|happened|happens|occur|"
    r"occurred|what|how|show|shows|shown|showed|reflected|recorded|reported|evident|"
    r"indicated|actual|actually|reality)\b|[\w'’‘]*\W+so\s*(?:[.!?;]|$)",
    re.IGNORECASE,
)
_DENIAL = re.compile(
    r"\b(?:untrue|incorrect|wrong|inaccurate|mistaken|mistake|erroneous|error|unfounded|"
    r"baseless|fabricated|bogus|misleading|disputed|doubtful|dubious|questionable|unverified|"
    r"unconfirmed|unsupported|false(?:ly)?|overstated|understated|exaggerated|made up|"
    r"opposite|contrary)\b",
    re.IGNORECASE,
)
_RESPONSE = re.compile(
    r"(?:no|not|nope|never|false|wrong|incorrect|untrue|hardly|neither|nor)\b[\s,]*"
    r"(?:at all|really|quite|so|true|exactly|the case|correct|accurate|right|indeed)?\s*"
    r"(?:[.!?,;]|$)",
    re.IGNORECASE,
)
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
    if op in _UNSURE:
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
_CUE = re.compile(_CUE_WORDS, re.IGNORECASE)
_CUE_WINDOW = 3  # words between a number and its cue word
_WORD_GAP = r"(?:[\s,()\-]*[\w'’.\-]+){0,%d}?[\s,()\-]*"
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
    r"(?:times\b|×|-?fold\b|[xX](?![A-Za-z0-9]))",  # "3 x" and "3 X" are multiples too
    re.IGNORECASE,
)
# The word right after a compared number: a unit, a function word or a threshold word keeps
# the clause's named field as its subject; any other word ("more than 50 earlier
# transactions") names something else, so the comparison's subject is unclear unless that
# word starts a field mention (B5.13 verifier pass 6).
_UNIT_AFTER = re.compile(
    r"(?:\s*(?:[xX×](?![A-Za-z0-9])|times\b|-?fold\b|%|percent\b|pct\b|per\s?cent\b|"
    r"dollars?\b|usd\b|eur\b|euros?\b|gbp\b|pounds?\b|cents?\b|points?\b|pts\b))?",
    re.IGNORECASE,
)
_WORD_AFTER = re.compile(r"\s+([A-Za-z][\w'’\-]*)")
_FUNCTION_TEXT = (
    "a an the this that these those its it it's their and or but nor so yet with without for "
    "of in on at by to from into onto than then as is are was were be been being am has have "
    "had which who whom whose while whereas though although because since if when where per "
    "vs versus via against compared relative over under above below plus each overall local "
    "only exactly approximately roughly about around nearly almost just well far much too "
    "also already still even now here there needed required allowed permitted set mark "
    "respectively"
)
_FUNCTION_WORDS = frozenset(_FUNCTION_TEXT.split())
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
    """One reason, read once: sentences, clause ends, negations, field mentions."""

    text: str
    sentences: list[int]  # start of each sentence
    clause_ends: list[tuple[int, int]]  # (start, end) of each clause end, in order
    negations: list[int]  # start of each negation word
    # negations that may deny a claim before them: not one joined to the next word by a hyphen
    # ("the 10x never-auto-clear threshold" names a rule; it denies nothing)
    predicates: list[int]
    mentions: list[_Mention]  # whole field names and aliases, in order, not overlapping
    partials: list[_Mention]  # one word of a field name that is not a whole mention
    boundaries: list[int]  # start of each clause boundary (comma, "which", "so", ...)
    denying: list[int]  # negations followed by a word of truth ("not true")
    denials: list[int]  # denial words ("untrue", "wrong")

    def __post_init__(self) -> None:
        self._end_starts = [s for s, _ in self.clause_ends]
        self._end_ends = [e for _, e in self.clause_ends]
        self._mention_starts = [m.start for m in self.mentions]
        self._partial_starts = [m.start for m in self.partials]

    def sentence_start(self, pos: int) -> int:
        return self.sentences[bisect.bisect_right(self.sentences, pos) - 1]

    def sentence_end(self, pos: int) -> int:
        """Where the sentence holding `pos` ends (the next sentence's start, or the end)."""
        i = bisect.bisect_right(self.sentences, pos)
        return self.sentences[i] if i < len(self.sentences) else len(self.text)

    def negated_between(self, start: int, end: int) -> bool:
        return _any_between(self.negations, start, end)

    def denied_after(self, end: int) -> bool:
        """A negation or denial after a claim ending at `end`, in its sentence, that may deny
        it (see `_BOUNDARY`), or a next sentence that is only a denial."""
        stop = self.sentence_end(end)
        i = bisect.bisect_left(self.predicates, end)
        if i < len(self.predicates) and self.predicates[i] < stop:
            first = self.predicates[i]
            if not _any_between(self.boundaries, end, first):
                return True  # "Amount above $800 is not true"
            gap = self.text[end:first].rstrip()
            last = len(gap) - 1 + end  # "812.40, not 900": the only boundary is that comma
            if gap[-1:] in (",", "(", "—", "–", "-") and not _any_between(
                self.boundaries, end, last
            ):
                return True
        if _any_between(self.denying, end, stop) or _any_between(self.denials, end, stop):
            return True
        return stop < len(self.text) and _RESPONSE.match(self.text, stop) is not None

    def clause(self, start: int, end: int) -> tuple[int, int]:
        """The clause holding the span [start, end)."""
        i = bisect.bisect_right(self._end_ends, start) - 1
        lo = self._end_ends[i] if i >= 0 else 0
        j = bisect.bisect_left(self._end_starts, end)
        hi = self._end_starts[j] if j < len(self._end_starts) else len(self.text)
        return lo, hi

    def candidates(
        self,
        start: int,
        end: int,
        fields: Mapping[str, Any],
        *,
        equal: bool = False,
        taken: Callable[[str, int], bool] | None = None,
    ) -> tuple[list[str], list[str]]:
        """(named, partly named): the fields whose whole name or alias the clause holds, and
        those of which it holds only one word ("card" of card_history_n), not already named. A
        word is not a mention (B5.13 verifier pass 6), but the field it may mean is a
        candidate: a comparison must hold for it too. Every mention before the span counts;
        one after it counts unless a clause boundary lies between ("under 50, and the merchant
        is known to the card"); a partial one after it only within `_PARTIAL_GAP` words ("above
        2.66 for risk"). For an equality (`equal`) a mention after "of" is what the
        number is a share of ("0.08 of the card's earlier purchases"), and a mention before is
        dropped when `taken(field, mention end)` says a number between already claimed it
        ("amount of 1899.38 ... only 8.8% of the time")."""
        text = self.text

        def keep(m: _Mention, before: bool, partial: bool = False) -> bool:
            if before:
                return not (equal and taken is not None and taken(m.field, m.end))
            gap = text[end : m.start]
            if _any_between(self.boundaries, end, m.start):
                return False
            if partial and len(re.findall(r"[A-Za-z]+", gap)) > _PARTIAL_GAP:
                return False  # "under 50 at a merchant the card has used": not its subject
            return not (equal and re.search(r"\bof\b", gap, re.IGNORECASE))

        def keep_partial(m: _Mention, before: bool) -> bool:
            return keep(m, before, partial=True)

        span = self.clause(start, end)
        named = _near(self.mentions, self._mention_starts, span, start, end, fields, keep)
        part = _near(self.partials, self._partial_starts, span, start, end, fields, keep_partial)
        return named, [f for f in part if f not in named]

    def mention_at(self, pos: int) -> bool:
        """A whole or partial field mention starts at `pos`."""
        for starts in (self._mention_starts, self._partial_starts):
            i = bisect.bisect_left(starts, pos)
            if i < len(starts) and starts[i] == pos:
                return True
        return False

    def last_mention_end(self, lo: int, pos: int) -> int:
        """The end of the last whole mention in [lo, pos), else lo."""
        i = bisect.bisect_left(self._mention_starts, pos) - 1
        while i >= 0 and self.mentions[i].end > pos:
            i -= 1
        return max(lo, self.mentions[i].end) if i >= 0 else lo


def _any_between(points: Sequence[int], start: int, end: int) -> bool:
    i = bisect.bisect_left(points, start)
    return i < len(points) and points[i] < end


def _near(
    mentions: Sequence[_Mention],
    starts: Sequence[int],
    span: tuple[int, int],
    start: int,
    end: int,
    fields: Mapping[str, Any],
    keep: Callable[[_Mention, bool], bool],
) -> list[str]:
    """Fields from `fields` mentioned in the clause `span` around [start, end): the nearest
    mention before first, then the following ones, nearest first (each field once; the
    nearest `_NEAREST` mentions each way are read)."""
    lo, hi = span
    mid = bisect.bisect_left(starts, start)
    before: list[str] = []
    for m in reversed(mentions[max(0, mid - _NEAREST) : mid]):
        if m.start < lo:
            break
        if m.end <= start and m.field in fields and keep(m, True):
            before.append(m.field)
    after: list[str] = []
    for m in mentions[mid : mid + _NEAREST]:
        if m.start >= hi:
            break
        if m.start >= end and m.end <= hi and m.field in fields and keep(m, False):
            after.append(m.field)
    return list(dict.fromkeys([*before, *after]))


_PARTIAL_GAP = 3  # words between a number and a partial mention after it that names its subject
# words of a key that are never read as a partial mention of it
_PARTIAL_STOP = frozenset({"the", "and", "for", "per", "num", "nbr", "its", "has", "was"})


def _mentions(
    text: str, record: Mapping[str, Any], aliases: Mapping[str, Sequence[str]]
) -> tuple[list[_Mention], list[_Mention]]:
    """(mentions, partial mentions). A mention is a record key's whole name as words ("risk
    score" for risk_score, "card history" for card_history_n: all its words of three letters
    or more, in order) or one of the caller's aliases. One word of a several-word key that is
    not inside a mention ("card", "risk", "history") is a partial mention of every key that has
    it; it never names a field on its own (B5.13 verifier pass 6)."""
    keys = list(dict.fromkeys([*(k for k in record if isinstance(k, str)), *aliases]))
    found: list[_Mention] = []
    for k in keys:
        pats = [_name_pattern(k)] if len(k) >= _MIN_NAME else []
        toks = _tokens(k)
        if len(toks) >= 2 and len(toks[-1]) < _MIN_NAME:
            pats.append(_name_pattern("_".join(toks[:-1])))  # "card history" for card_history_n
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
    covered = _cover(len(text), [(m.start, m.end) for m in kept])
    words: dict[str, list[str]] = {}
    for k in keys:
        toks = _tokens(k)
        if len(toks) < 2:
            continue
        for t in dict.fromkeys(toks):
            if len(t) >= _MIN_NAME and t not in _PARTIAL_STOP and not t.isdigit():
                words.setdefault(t, []).append(k)
    partial: list[_Mention] = []
    for word, owners in words.items():
        pat = _name_pattern(word)
        if pat is None:
            continue
        for m in pat.finditer(text):
            if not (covered[m.start()] or covered[m.end() - 1]):
                partial += [_Mention(m.start(), m.end(), k) for k in owners]
    partial.sort(key=lambda m: (m.start, m.field))
    return kept, partial


def _read(text: str, record: Mapping[str, Any], aliases: Mapping[str, Sequence[str]]) -> _Text:
    mentions, partials = _mentions(text, record, aliases)
    negations: list[int] = []
    predicates: list[int] = []
    for m in _ANY_NEGATION.finditer(text):
        negations.append(m.start())
        if not re.match(r"-\w", text[m.end() : m.end() + 2]):
            predicates.append(m.start())
    ends = [m.end for m in mentions]
    denials = []
    for m in _DENIAL.finditer(text):
        # "merchant_first_seen is false" states a field's value, not a denial
        if m.group(0).lower().startswith("false"):
            k = bisect.bisect_right(ends, m.start()) - 1
            if k >= 0 and m.start() - ends[k] <= 12:
                continue
        denials.append(m.start())
    return _Text(
        text=text,
        sentences=[0] + [m.end() for m in _SENTENCE_END.finditer(text)],
        clause_ends=[(m.start(), m.end()) for m in _CLAUSE_END.finditer(text)],
        negations=negations,
        predicates=predicates,
        mentions=mentions,
        partials=partials,
        boundaries=[m.start() for m in _BOUNDARY.finditer(text)],
        denying=[p for p in predicates if _TRUTH_AFTER.match(text, p)],
        denials=denials,
    )


@dataclass(frozen=True)
class _Time:
    start: int
    end: int
    hour: int
    minute: int
    word: bool = False  # "midnight", "noon": only ever the end of a window


@dataclass(frozen=True)
class _Window:
    start: int
    end: int
    opens: tuple[float, ...]  # the hour it opens at; two readings for a bare "10 to 6 a.m."
    closes: float


def _times(text: str) -> tuple[list[_Time], list[_Window]]:
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
    windows: list[_Window] = []
    in_window: set[int] = set()
    for i, t in enumerate(found):
        nxt = found[i + 1] if i + 1 < len(found) else None
        if i in in_window:
            continue
        if nxt is not None and _RANGE_JOIN.fullmatch(text, t.end, nxt.start):
            opens = (t.hour + t.minute / 60,)
            windows.append(_Window(t.start, nxt.end, opens, nxt.hour + nxt.minute / 60))
            in_window.update((i, i + 1))
        elif not t.word and (bare := _BARE_HOUR.search(text, max(0, t.start - 20), t.start)):
            h = int(re.match(r"\d+", bare.group(0)).group(0))  # type: ignore[union-attr]
            opens = tuple(sorted({float(h % 12), float(h % 12 + 12)})) if h else (0.0,)
            windows.append(_Window(bare.start(), t.end, opens, t.hour + t.minute / 60))
            in_window.add(i)  # "10 to 6 a.m."
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


# "above the 5x signal (P3.1) and the 10x limit", "above both 800 and 900": a number joined by
# "and" (or "and the") to a compared
# one of the same kind (both multiples, or neither), with no comparison word, field name or
# number between, shares its comparison
_COORDINATED = re.compile(
    r"(?:\s*(?:x|×|times|%))?(?:(?!\b(?:" + _CMP_WORDS.replace("|>=|<=|≥|≤|>|<", "") + r")\b)"
    r"[^.;:\d<>≤≥])"
    r"{0,60}?(?:\(P?[\d.]+\)[^.;:\d<>≤≥]{0,20})?,?\s+and\s+(?:the\s+)?",
    re.IGNORECASE,
)
# "for 801.52, which is at least 200": a comparison right after a number refers back to it
_REFERS_BACK = re.compile(
    r"(?:\s*(?:x|×|times|%|dollars|usd))?[\s,()]*(?:which|that)\s+(?:is|was)\b[^.;:\d]{0,40}",
    re.IGNORECASE,
)


# (comparison, field, end of claim, a multiple?, the field named rather than matched?)
_Claim = tuple[str, str | None, int, bool, bool]


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
    """A number checked as `op` against the field `name` (None: no field backs it). A plain
    number that no field backs is a mismatch (it fails); a comparison that no field backs has
    no subject the record can settle ("below the 10x limit"): `no_field`, unverifiable."""
    check: dict[str, Any] = {"text": text, "kind": kind, "field": name, "op": op}
    if via is not None:
        check["via"] = via
    check["claimed"] = _claimed(n)
    if name is None:
        if op in _NEGATE:
            return {**check, "found": None, "ok": False, "reason": "no_field"}
        return {**check, "found": None, "ok": False}
    try:
        value = _numeric(record, name, where)
    except RuleError:  # a field of the wrong type: unchecked (type_error); never raises
        return {**check, "found": None, "ok": False, "reason": f"type_error:{name}"}
    ok = value is not None and _finite(n) and _claim(n, op, value, tolerance)
    check.update(found=value, ok=ok)
    return {**check, "reason": f"missing_value:{name}"} if value is None else check


def _every(results: Sequence[dict[str, Any]], partly: bool = False) -> dict[str, Any]:
    """One check over several candidate fields (B5.13 verifier pass 6): ok when it holds for
    every candidate, failed when it fails for every one on a clear reading, else ambiguous
    (`unclear_field`). `partly`: the clause only partly names its field ("card" for
    card_history_n), so it is ambiguous whatever the values."""
    out = dict(results[0])
    if len(results) > 1:
        out["candidates"] = {r["field"]: r["found"] for r in results}
    statuses = {_status(r) for r in results}
    if partly:
        out.update(ok=False, reason="unclear_field")
    elif statuses == {"ok"}:
        out["ok"] = True
    elif statuses == {"failed"}:
        out["ok"] = False
    elif statuses <= {"ok", "failed"}:
        out.update(ok=False, reason="unclear_field")
    else:  # a candidate the record cannot settle (missing, wrong type) or an unsure reading
        bad = next(r for r in results if _status(r) in ("unchecked", "ambiguous"))
        why = bad.get("reason") or _UNSURE_REASON.get(bad.get("op") or "", "unclear_field")
        out.update(ok=False, reason=why)
    return out


def _hour_span(value: float) -> tuple[float, float]:
    """The times an hour field allows: a whole hour v is [v, v + 1) (minutes unknown); any
    other value is that instant."""
    return (value, value + 1.0) if float(value).is_integer() else (value, value)


def _over(a: float, b: float, op: str, c: float) -> bool | None:
    """`t op c` for every t in [a, b) (True), for none (False), or for some (None)."""
    if a == b:
        return {">": a > c, ">=": a >= c, "<": a < c, "<=": a <= c}[op]
    every = {">": a > c, ">=": a >= c, "<": b <= c, "<=": b <= c}[op]
    none = {">": b <= c, ">=": b <= c, "<": a >= c, "<=": a > c}[op]
    return True if every else False if none else None


def _time_check(
    *, text: str, t: _Time, op: str, name: str, record: Mapping[str, Any], where: str
) -> dict[str, Any]:
    """A time of day checked against an hour field. A whole hour v allows any minute of it:
    "3 a.m." equals hour 3, "3:45 a.m." cannot be settled by it, "before 3:30 a.m." neither
    (`unclear_time`); "after 10 p.m." holds for hour 23 and fails for hour 21."""
    claimed = t.hour + t.minute / 60
    check: dict[str, Any] = {"text": text, "kind": "time", "field": name, "op": op}
    check["claimed"] = claimed
    try:
        value = _numeric(record, name, where)
    except RuleError:
        return {**check, "found": None, "ok": False, "reason": f"type_error:{name}"}
    if value is None:
        return {**check, "found": None, "ok": False, "reason": f"missing_value:{name}"}
    if op in _UNSURE:
        return {**check, "found": value, "ok": False}
    if op == "≈":
        a, b = _hour_span(value)
        if math.floor(value) != t.hour:
            holds: bool | None = False
        elif t.minute == 0:
            holds = True
        else:
            holds = abs(value - claimed) < 1 / 120 if a == b else None
    else:
        holds = _over(*_hour_span(value), op, claimed)
    if holds is None:
        return {**check, "found": value, "ok": False, "reason": "unclear_time"}
    return {**check, "found": value, "ok": holds}


# how a reason places an hour against a window: "falls in", "inside", "outside", "after"
_RELATIONS = (
    ("in", re.compile(r"\b(?:in|inside|within|during|between|into)\b", re.IGNORECASE)),
    ("out", re.compile(r"\b(?:outside|beyond)\b|\bout\s+of\b", re.IGNORECASE)),
    ("after", re.compile(r"\b(?:after|past)\b|\blater\s+than\b", re.IGNORECASE)),
    ("before", re.compile(r"\bbefore\b|\b(?:earlier\s+than|prior\s+to)\b", re.IGNORECASE)),
)


def _inside(a: float, b: float, opens: float, closes: float) -> bool | None:
    """The times [a, b) all inside the window (True), all outside (False), or some (None)."""
    if not 0 <= a < 24 or opens == closes:
        return None
    segs = [(opens, closes)] if opens < closes else [(opens, 24.0), (0.0, closes)]
    if a == b:
        return any(lo <= a < hi for lo, hi in segs)
    if any(lo <= a and b <= hi for lo, hi in segs):
        return True
    if all(b <= lo or a >= hi for lo, hi in segs):
        return False
    return None


def _placed(relation: str, value: float, opens: float, closes: float) -> bool | None:
    a, b = _hour_span(value)
    if relation in ("in", "out"):
        inside = _inside(a, b, opens, closes)
        return None if inside is None else inside == (relation == "in")
    if opens >= closes:
        return None  # "after" a window across midnight: unclear
    return _over(a, b, ">=", closes) if relation == "after" else _over(a, b, "<", opens)


def _window_check(
    read: _Text,
    w: _Window,
    names: Sequence[str],
    record: Mapping[str, Any],
    partly: bool,
    relations: Mapping[str, Sequence[int]],
) -> dict[str, Any]:
    """A window ("10 p.m. to 6 a.m.") checked against the hour fields its clause names, when
    the clause says how the hour sits against it ("falls in", "inside", "outside", "after";
    only one kind of word). A negation before it in the sentence, or one after it that may deny
    it, makes it ambiguous; so does a bare first hour whose two readings ("10 to 6 a.m.": 10 or
    22) disagree, or an hour whose minutes would decide it."""
    text = read.text
    lo = max(read.clause(w.start, w.end)[0], read.sentence_start(w.start))
    kinds = {kind for kind, at in relations.items() if _any_between(at, lo, w.start)}
    relation = next(iter(kinds)) if len(kinds) == 1 else None
    if read.negated_between(read.sentence_start(w.start), w.start) or read.denied_after(w.end):
        relation, why = None, "ambiguous_negation"
    else:
        why = "unclear_time"
    results: list[dict[str, Any]] = []
    for name in names:
        check: dict[str, Any] = {
            "text": text[w.start : w.end],
            "kind": "time_window",
            "field": name,
            "op": relation or AMBIGUOUS,
            "claimed": [w.opens[0] if len(w.opens) == 1 else list(w.opens), w.closes],
        }
        try:
            value = _numeric(record, name, "time window")
        except RuleError:
            results.append({**check, "found": None, "ok": False, "reason": f"type_error:{name}"})
            continue
        if value is None:
            results.append({**check, "found": None, "ok": False, "reason": f"missing_value:{name}"})
            continue
        readings = {_placed(relation, value, o, w.closes) for o in w.opens} if relation else {None}
        holds = next(iter(readings)) if len(readings) == 1 else None
        if holds is None:
            results.append({**check, "found": value, "ok": False, "reason": why})
        else:
            results.append({**check, "found": value, "ok": holds})
    return _every(results, partly)


def _holds_negation(read: _Text, start: int, end: int) -> str | None:
    """How a negation reads a condition phrase at [start, end): "invert" when one sits
    directly before it ("not a first purchase"), AMBIGUOUS when another is earlier in the
    sentence or one after it may deny it ("... is not the case"), else None."""
    sentence = read.sentence_start(start)
    window = read.text[max(sentence, start - _WINDOW_BEFORE) : start]
    direct = _HOLDS_NEGATION.search(window)
    cut = start - len(window) + direct.start() if direct else start
    if read.negated_between(sentence, cut) or read.denied_after(end):
        return AMBIGUOUS
    return "invert" if direct else None


def _subject_after(read: _Text, end: int) -> bool:
    """A word right after a compared number names something other than the clause's field
    ("more than 50 earlier transactions"): not a unit, function word or threshold word, and
    no field mention starts there."""
    text = read.text
    pos = _UNIT_AFTER.match(text, end)
    word = _WORD_AFTER.match(text, pos.end() if pos else end)
    if word is None or read.mention_at(word.start(1)):
        return False
    is_function = word.group(1).lower() in _FUNCTION_WORDS
    return not (is_function or _CUE_AFTER.match(text[end : end + 60]))


_BLOCKING = frozenset({"time", "time_window", "constant"})  # unchecked: never verified


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
    unchecked: list[dict[str, Any]] = []
    timed = _cover(
        len(text), [*((w.start, w.end) for w in windows), *((t.start, t.end) for t in points)]
    )
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
    # where the number before each one ends: a comparison word before that is not this one's
    floors = [0] + [n.start + len(n.text) for n in numbers[:-1]]
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
                negation = _holds_negation(read, m.start(), m.end())
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
                op = _comparison(
                    text, read, t.start, t.end, time=True, floor=floors[i], claim_end=m.end()
                )
                checks.append(
                    _time_check(text=span, t=t, op=op, name=ph.field, record=record, where=where)
                )
                continue
            op = _comparison(
                text, read, n.start, n.start + len(n.text), floor=floors[i], claim_end=m.end()
            )
            resolved[i] = (op, ph.field, m.end(), n.multiple or "times" in span.lower(), True)
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
    # where each relation word ("in", "outside", ...) is, read once
    relations = {kind: [m.start() for m in pat.finditer(text)] for kind, pat in _RELATIONS}
    for w in windows:
        named, partly = read.candidates(w.start, w.end, hours)
        if not (named or partly):
            unchecked.append({"text": reason[w.start : w.end], "kind": "time_window"})
            continue
        names = [*named, *partly]
        checks.append(_window_check(read, w, names, record, not named, relations))
    for i, n in enumerate(numbers):
        if i in used:
            continue
        end = n.start + len(n.text)
        if i in time_of:
            t = time_of[i]
            named, partly = read.candidates(t.start, t.end, hours)
            if not (named or partly):
                unchecked.append({"text": n.text, "kind": "time"})
                continue
            op = _comparison(text, read, t.start, t.end, time=True, floor=floors[i])
            results = [
                _time_check(text=n.text, t=t, op=op, name=k, record=record, where="time of day")
                for k in [*named, *partly]
            ]
            checks.append(_every(results, partly=not named))
            continue
        op = _comparison(text, read, n.start, end, floor=floors[i])
        multiple = n.multiple or _TIMES_AFTER.match(text, end) is not None
        fields = ratios if multiple else nameable
        pool = {k: v for k, v in numeric.items() if k in ratios} if multiple else numeric
        prev = _previous(read, numbers, resolved, i)
        if (
            op == "≈"
            and prev is not None
            and prev[3] == multiple
            and _COORDINATED.fullmatch(text, prev[2], n.start)
        ):
            op = prev[0]  # "above the 5x signal and the 10x limit": above both
        if op == "≈":

            def taken(field: str, after: int, i: int = i) -> bool:
                # a number between the mention and this one already claimed the field by name
                return any(
                    j in resolved and resolved[j][1] == field and resolved[j][4]
                    for j in range(bisect.bisect_left(starts, after), i)
                )

            named, partly = read.candidates(n.start, end, fields, equal=True, taken=taken)
            # a value may equal any field the clause names, even one it is a share of
            every, every_partly = read.candidates(n.start, end, fields)
            matches = list(dict.fromkeys([*named, *partly, *every, *every_partly]))
        else:
            named, partly = read.candidates(n.start, end, fields)
            matches = []
        cands = [*named, *partly]

        def number_check(
            name: str | None, via: str | None, op: str = op, n: Number = n
        ) -> dict[str, Any]:
            return _value_check(
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

        check: dict[str, Any]
        if op == "≈":
            # equal: a candidate field that matches; when the clause names fields and none
            # matches, a match elsewhere is unclear (which field is it?) and no match fails
            name = next(
                (k for k in matches if k in numeric and _close(n, numeric[k], tolerance)), None
            )
            other = next((k for k, v in pool.items() if _close(n, v, tolerance)), None)
            if name is not None:
                check = number_check(name, "name")
            elif _constant(n, constants, read, end):
                # a policy threshold stated as such ("the 5x signal"), not an alert fact; it is
                # recorded and its reason is never verified (B5.13 verifier pass 6)
                unchecked.append({"text": n.text, "kind": "constant", "value": _claimed(n)})
                continue
            elif named and other is not None:
                check = {**number_check(other, "value"), "ok": False, "reason": "unclear_field"}
            elif named:
                check = number_check(named[0], "name")
            else:
                # no field named: the first field within tolerance (B5.13 v0.3 reading); a
                # multiple only among the ratio fields
                check = number_check(other, "value")
        elif op in _UNSURE:
            check = number_check(cands[0] if cands else None, "name")
        elif _subject_after(read, end):
            # "more than 50 earlier transactions": the number talks about something else
            check = {**number_check(cands[0] if cands else None, "name"), "ok": False}
            check["reason"] = "unclear_subject"
        elif named:
            # a comparison names its field: it must hold for every field the clause names
            # (B5.13 verifier pass 6), even when far from the number
            check = _every([number_check(k, "name") for k in cands])
        elif partly:
            check = {**number_check(partly[0], "name"), "ok": False, "reason": "unclear_field"}
        elif (
            prev is not None
            and prev[1] in fields
            and _REFERS_BACK.fullmatch(text, prev[2], n.start)
        ):
            # "for 801.52, which is at least 200": the field the previous number claimed
            check = number_check(prev[1], "reference")
        else:
            # a comparison naming no field has no subject the record can settle (`no_field`):
            # never guessed from a field near in value (B5.13 verifier pass 6)
            check = number_check(None, None)
        resolved[i] = (op, check.get("field"), end, multiple, check.get("via") == "name")
        checks.append(check)
    for c in checks:
        if "reason" not in c and c.get("op") == AMBIGUOUS:
            c["reason"] = "ambiguous_negation"
        elif "reason" not in c and c.get("op") == UNCLEAR:
            c["reason"] = "unclear_comparison"
        c["status"] = _status(c)
    return checks, unchecked


Outcome = Literal["verified", "contradicted", "unverifiable"]
OUTCOMES: tuple[Outcome, ...] = ("verified", "contradicted", "unverifiable")


def _status(check: Mapping[str, Any]) -> str:
    """ok; failed (false on a clear reading); ambiguous (a reading the checker is not sure of:
    `ambiguous_negation`, or `unclear_comparison`, `unclear_field`, `unclear_subject`,
    `unclear_time`); unchecked (the record cannot settle it: missing_value, type_error, or
    no_field, a comparison whose subject is no field)."""
    if check["ok"]:
        return "ok"
    why = check.get("reason") or ""
    if why == "ambiguous_negation" or why.startswith("unclear_") or check.get("op") in _UNSURE:
        return "ambiguous"
    if why == "no_field" or why.startswith(("missing_value:", "type_error:")):
        return "unchecked"
    return "failed"


def _outcome(
    checks: Sequence[Mapping[str, Any]], unchecked: Sequence[Mapping[str, Any]] = ()
) -> Outcome:
    """D-72: contradicted when any check failed (even if others are ambiguous); verified when
    there is at least one check, every one is ok, and no time, window or constant was left
    unchecked (B5.13 verifier pass 6); else unverifiable."""
    statuses = {c["status"] for c in checks}
    if "failed" in statuses:
        return "contradicted"
    if any(u["kind"] in _BLOCKING for u in unchecked):
        return "unverifiable"
    return "verified" if statuses == {"ok"} else "unverifiable"


def _comparison(
    text: str,
    read: _Text,
    start: int,
    end: int,
    *,
    time: bool = False,
    floor: int = 0,
    claim_end: int | None = None,
) -> str:
    """The comparison stated around the number at [start, end), else "≈" (equal): a word
    right before it ("above 900", "exceeds the threshold of 0.9", "above USD 850"), inverted
    by a negation directly before that word ("not above 900", "doesn't exceed 900"), or a
    phrase right after it ("900 or less"). Any other negation earlier in the same sentence,
    however far back, or one after the claim that may deny it ("... is not true"), makes it
    AMBIGUOUS. A comparison word elsewhere between the number before (or the clause start, or
    the last field named) and this one ("above the policy cap at 850") makes it UNCLEAR."""
    sentence = read.sentence_start(start)
    window = text[max(sentence, start - _COMPARATOR_WINDOW) : start]
    cmp = (_TIME_COMPARATOR if time else _COMPARATOR).search(window)
    if cmp:
        op = _OP[re.sub(r"\s+", " ", cmp.group("cmp").lower())]
        first = cmp.start("neg") if cmp.group("neg") else cmp.start("cmp")
        if read.negated_between(sentence, start - len(window) + first) or read.negated_between(
            start - len(window) + cmp.end("cmp"), start
        ):
            # a negation not directly before the comparison, or one between the comparison and
            # its number ("above the never-auto-clear limit of 10")
            return AMBIGUOUS
        op = _NEGATE[op] if cmp.group("neg") else op
    elif read.negated_between(sentence, start):
        # a negation before a number with no recognised comparison ("not-above $800",
        # "not even close to above $800") is not read as an equality claim either
        return AMBIGUOUS
    elif post := _POSTFIX.match(text[end : end + _COMPARATOR_WINDOW]):
        op = _POSTFIX_OP[post.group("word").lower()]
    else:
        lo = read.last_mention_end(max(read.clause(start, end)[0], sentence, floor), start)
        loose = _LOOSE_TIME if time else _LOOSE_COMPARISON
        op = UNCLEAR if loose.search(text, lo, start) else "≈"
    if read.denied_after(end if claim_end is None else claim_end):
        return AMBIGUOUS
    return op


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
    """Check every number and declared phrase in `reason` against `record`.

    Outcome (D-72), in `details["outcome"]`: `contradicted` when any check failed on a clear
    reading (a false claim, even if other checks are ambiguous); `verified` when there is at
    least one check, every check is ok, and no time, window or constant was read but left
    unchecked; else `unverifiable`. The value is 1.0 only when verified, else 0.0. Each check
    carries `status`: `ok`, `failed`, `ambiguous` (`ambiguous_negation`, `unclear_field`,
    `unclear_subject`, `unclear_comparison`, `unclear_time`) or `unchecked`
    (`missing_value:<field>`, `type_error:<field>`, `no_field`). Details also hold `checks`,
    `failed` (the text of each failed check), `unverified` (the text of each ambiguous or
    unchecked check, then of each unchecked time, window or constant), `n_checked`, and
    `unchecked` (times, windows, rule ids, constants and ignored spans read but not checked;
    they never contradict).

    A number written with a minus must match a negative value; an unsigned number ("fell
    0.83") is compared with the value's magnitude. A comparison ("risk score is below 0.9",
    "amount over 200") is checked against the fields its clause names: a record key's whole
    name as words ("risk score", "card history" for card_history_n) or one of `aliases`
    (field -> phrases); one shared word ("card", "risk") is not a mention but its field is a
    candidate. It is ok only when it holds for every candidate, failed only when it fails for
    every one, else `unclear_field`; a word after the number naming something else ("50
    earlier transactions") makes it `unclear_subject`. A multiple ("5x", "5 X", "5 times")
    names only a ratio field ("amt_vs_usual"). "...and the 10x limit" after "above the 5x
    signal" shares its comparison, as "both 800 and 900" does; ", which is at least 200"
    refers to the number before it. A comparison naming no field is unchecked (`no_field`).
    A comparison word in the clause not directly before the number, in no shape read ("the
    threshold of 0.9", "USD 850"), makes it `unclear_comparison`. A plain number is ok when a
    candidate field equals it within the displayed precision or `tolerance` (with no field
    named: any field); when the clause names fields and none equals it, a match elsewhere is
    `unclear_field` and no match fails.

    Times of day ("10 p.m.", "6am", "22:00") are not free numbers: one is checked against an
    hour field (a key with the word "hour") named in its clause, else recorded as unchecked;
    a whole hour leaves minutes unknown (`unclear_time`). A window ("10 p.m. to 6 a.m.") is
    checked against a named hour field when the clause says how the hour sits against it
    ("falls in", "outside"), else recorded as unchecked. A number equal to one of `constants`
    with a threshold word next to it ("the 5x signal", "threshold of 0.9") is a policy
    constant, recorded as unchecked, unless it is compared or equals a field its clause names.
    Numbers after "rule", "clause" or "section" are ids; ids like "P7.1" are never numbers.
    Numbers inside spans matched by an `ignore` regex are recorded as unchecked (negations
    and names there are still read).

    A negation directly before a comparison or a condition phrase inverts it ("not above 900",
    "not a first purchase at this merchant"); any other negation earlier in the same sentence,
    or one after the claim that may deny it ("... is not true", "812.40, not 900"), makes the
    check `ambiguous` (`reason` `ambiguous_negation`). A field the record lacks, or a missing
    value, leaves the claim that needs it `unchecked`, with `reason`
    `missing_value:<field>`; neither is ever verified."""
    if not 0 <= tolerance < 1:
        raise ValueError("tolerance must be in [0, 1)")
    opts = _options(aliases, constants, ignore)
    checks, unchecked = _check_reason(
        reason, record, phrases, tolerance, aliases=opts[0], constants=opts[1], ignore=opts[2]
    )
    return _result(checks, unchecked)


def _result(checks: list[dict[str, Any]], unchecked: list[dict[str, Any]]) -> ScoreResult:
    outcome = _outcome(checks, unchecked)
    return ScoreResult(
        value=1.0 if outcome == "verified" else 0.0,
        details={
            "outcome": outcome,
            "checks": checks,
            "failed": [c["text"] for c in checks if c["status"] == "failed"],
            # ambiguous and unchecked checks, then unchecked times, windows and constants
            "unverified": [c["text"] for c in checks if c["status"] in ("ambiguous", "unchecked")]
            + [u["text"] for u in unchecked if u["kind"] in _BLOCKING],
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
    its reason.

    Each reason gets its own `value_check` outcome, listed in `details["reasons"]` (index,
    outcome, n_checked), counted in `counts` and divided by `n_reasons` in `shares` (None with
    no reasons). The case's `outcome` is contradicted when any reason is, verified when every
    reason is (at least one), else unverifiable; the value is 1.0 only when the case is
    verified, else 0.0, so a run's mean is the share of cases whose every reason was verified.
    `value_check_shares` turns the results into per-reason shares across a run. A failed call
    scores 0.0 with no outcome."""
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
        per_reason: list[dict[str, Any]] = []
        for k, reason in enumerate(reasons):
            got, skipped = _check_reason(
                reason, record, fixed, tolerance, aliases=al, constants=consts, ignore=patterns
            )
            # a check's own `reason` (missing_value, type_error, ambiguous_negation) is kept
            checks += [{**c, "reason_index": k} for c in got]
            unchecked += [{**c, "reason_index": k} for c in skipped]
            per_reason.append(
                {"index": k, "outcome": _outcome(got, skipped), "n_checked": len(got)}
            )
        counts = {o: sum(r["outcome"] == o for r in per_reason) for o in OUTCOMES}
        n = len(per_reason)
        outcome: Outcome = "unverifiable"
        if counts["contradicted"]:
            outcome = "contradicted"
        elif n and counts["verified"] == n:
            outcome = "verified"
        details = dict(_result(checks, unchecked).details)
        details |= {
            "outcome": outcome,
            "reasons": per_reason,
            "n_reasons": n,
            "counts": counts,
            "shares": {o: counts[o] / n for o in OUTCOMES} if n else None,
        }
        return ScoreResult(value=1.0 if outcome == "verified" else 0.0, details=details)

    run.__doc__ = value_check_scorer.__doc__
    return register_task_scorer(run, name, version)


class ValueCheckShares(Model):
    """Per-reason value_check outcomes across a run (D-72): each count and its share."""

    n_reasons: NonNegInt
    verified: NonNegInt
    contradicted: NonNegInt
    unverifiable: NonNegInt
    nothing_checked: NonNegInt  # the unverifiable reasons with no check at all
    n_skipped: NonNegInt  # results with no value_check outcome (a failed call)
    verified_share: Interval | None  # None when there are no reasons
    contradicted_share: Interval | None
    unverifiable_share: Interval | None
    method: Literal["clopper-pearson"] = "clopper-pearson"


def value_check_shares(results: Iterable[ScoreResult], *, alpha: float = 0.05) -> ValueCheckShares:
    """Count the reasons in `value_check` or `value_check_scorer` results by outcome; each
    share of all reasons gets a (1 - alpha) Clopper-Pearson interval. A result without an
    outcome (a failed call) is counted in `n_skipped`."""
    if not 0 < alpha < 1:
        raise ValueError("alpha must be in (0, 1)")
    counts: dict[str, int] = dict.fromkeys(OUTCOMES, 0)
    nothing = skipped = 0
    for r in results:
        d: Mapping[str, Any] = r.details
        if "reasons" in d:  # a value_check_scorer result: one row per reason
            rows: Sequence[Mapping[str, Any]] = d["reasons"]
        elif "outcome" in d:  # one value_check result
            rows = [{"outcome": d["outcome"], "n_checked": d["n_checked"]}]
        else:
            skipped += 1
            continue
        for row in rows:
            outcome = row["outcome"]
            if outcome not in counts:
                raise ValueError(f"unknown value_check outcome {outcome!r}")
            counts[outcome] += 1
            nothing += outcome == "unverifiable" and row["n_checked"] == 0
    n = sum(counts.values())
    share = {o: clopper_pearson(counts[o], n, alpha) if n else None for o in OUTCOMES}
    return ValueCheckShares(
        n_reasons=n,
        verified=counts["verified"],
        contradicted=counts["contradicted"],
        unverifiable=counts["unverifiable"],
        nothing_checked=nothing,
        n_skipped=skipped,
        verified_share=share["verified"],
        contradicted_share=share["contradicted"],
        unverifiable_share=share["unverifiable"],
    )
