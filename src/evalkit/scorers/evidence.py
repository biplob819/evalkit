"""Structured evidence (SEV-01): judge a decision's reasons against the tabular record it was
made on.

Two parts, built for fraud-review-bedrock (Tech Spec §5.5 and EVK-02, MIT licence,
Copyright (c) 2026 Biplob Chakraborty, the same author) and general to any decision system:

- `render_evidence` turns a record into numbered evidence sentences, so `faithfulness()` can
  judge each reason against the record exactly as it judges an explanation against a document
  (sentence i of the list is evidence sentence i; the judge prompt numbers them).
- `value_check` checks the numbers in a reason against the record without a model, strictly
  (B5.16, D-74): a number is checked only when the text binds it to one field by positive
  evidence (the field's whole name or an alias, in a recognised shape such as "risk score is
  0.83", "amount above 800", "40 earlier purchases", or a declared `ValuePhrase`); any other
  number makes its reason unverifiable, and no number is ever matched to a field because its
  value is close. Each reason is `verified`, `contradicted` or `unverifiable` (D-72), and
  `value_check_shares` gives each share across a run with its count and an exact interval.

Templates read plain field names only; conditions use the RUL-01 predicate grammar. Nothing
from a record, template, phrase or reason is ever executed.
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from dataclasses import field as dc_field
from fractions import Fraction
from typing import Any, Literal

from pydantic import model_validator

from evalkit.schemas import Case, Completion, Interval
from evalkit.schemas._base import Model, Name, NonBlank, NonNegInt
from evalkit.scorers.base import Scorer, ScoreResult
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


# ------------------------------------------------------------------ value check (B5.16)


class ValuePhrase(Model):
    """A phrase in a reason and what it claims: a field's value (`field`, the one number
    inside the match is bound to that field) or a condition on the record (`holds`, a predicate
    in the rule grammar). `pattern` is a regex, case-folded."""

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


Outcome = Literal["verified", "contradicted", "unverifiable"]
OUTCOMES: tuple[Outcome, ...] = ("verified", "contradicted", "unverifiable")
Unit = Literal["fraction", "percent", "multiple", "currency", "hour"]
UNITS: tuple[str, ...] = ("fraction", "percent", "multiple", "currency", "hour")

# A displayed-precision match more than this share off the value is a coarse rounding
# ("risk score is 1" on 0.83): unverifiable, neither verified nor contradicted.
_COARSE = Fraction(1, 10)
# ... and at most this far off, absolute or relative (fraud-review-bedrock's D-42 tolerance)
_ABS_TOL = Fraction(5, 1000)
_REL_TOL = Fraction(5, 1000)

# ---- the words read (all lower case)


def _words(text: str) -> frozenset[str]:
    return frozenset(text.split())


# field <conn> number
_EQ_PHRASES: tuple[tuple[str, ...], ...] = (
    ("came", "in", "at"),
    ("is", "equal", "to"),
    ("was", "equal", "to"),
    ("is", "at"),
    ("was", "at"),
    ("stands", "at"),
    ("stood", "at"),
    ("is",),
    ("was",),
    ("equals",),
    ("of",),
    ("at",),
    ("totals",),
    ("totaled",),
    ("totalled",),
)
# comparison words directly before a number
_CMP_PHRASES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("greater", "than", "or", "equal", "to"), ">="),
    (("more", "than", "or", "equal", "to"), ">="),
    (("less", "than", "or", "equal", "to"), "<="),
    (("at", "or", "above"), ">="),
    (("at", "or", "below"), "<="),
    (("more", "than"), ">"),
    (("greater", "than"), ">"),
    (("higher", "than"), ">"),
    (("less", "than"), "<"),
    (("fewer", "than"), "<"),
    (("lower", "than"), "<"),
    (("at", "least"), ">="),
    (("at", "most"), "<="),
    (("above",), ">"),
    (("over",), ">"),
    (("exceeds",), ">"),
    (("exceeded",), ">"),
    (("exceeding",), ">"),
    (("exceed",), ">"),
    (("below",), "<"),
    (("under",), "<"),
    ((">",), ">"),
    (("<",), "<"),
    ((">=",), ">="),
    (("<=",), "<="),
    (("≥",), ">="),
    (("≤",), "<="),
)
# "or more", "and above" after a number: not read as a comparison (the reason is ambiguous)
_POSTFIX = _words("more higher above greater over up less lower below fewer under")
# before a field named first (or the start, or punctuation)
_PRE_WORDS = _words(
    "and but while whereas with so because since as when where at then also which that"
)
# a determiner may stand between: "... and the amount is 812.40" (never "risk of the amount")
_DETERMINERS = frozenset(("the", "this", "a", "an"))
# before a number whose field is named after it ("has 40 earlier purchases")
# before "[<cmp>] <n> <field>": the start, punctuation, a joining word, or "<subject> has"
# whose subject is a word of that field's own name ("The card has 40 earlier purchases")
_NUM_PRE_WORDS = _words("and but with while whereas so which")
_HAS = _words("has had have")
_SENTENCE_END = frozenset(".!?;")
# a hedge anywhere in a reason: nothing in it is verified or contradicted (verify516)
_HEDGE_WORDS = _words("roughly about approximately approx around nearly almost ballpark circa")
_HEDGE_PHRASES: tuple[tuple[str, ...], ...] = (
    ("give", "or", "take"),
    ("more", "or", "less"),
    ("or", "so"),
    ("or", "thereabouts"),
)
# after a bound number (or after the field named after it)
_FOLLOW = _words("and but while whereas so which is was are were")
_COORD = frozenset({"and", "or", "&", "/", ","})
_PRE_PUNCT = frozenset(".,;:!?([—–•*")
_CLEAN_PUNCT = frozenset(".,;:!?)]([—–")
_HEDGES = _words(
    "about around roughly nearly almost approximately approx circa near some just upwards "
    "excess north south shy short close to up within between from by than ~ ≈ + - − ±"
)
_NEGATIONS = _words(
    "not no never none neither nor without cannot nothing nobody nowhere hardly barely "
    "scarcely lacks lack lacking non"
)
_HYPOTHETICAL = _words(
    "if unless would could might may should will whether suppose supposing assume assuming "
    "expected expect estimate estimated predicted projected hypothetical hypothetically "
    "previously formerly prior last ago yesterday tomorrow historically requires require "
    "required requiring needs need needed must when otherwise fits fit criteria criterion "
    "condition conditions eligible allows allow permits permit permitted"
)
_HYPOTHETICAL_STEMS = ("satisf", "qualif")
_DENIALS = _words(
    "false untrue incorrect incorrectly wrong wrongly inaccurate mistaken mistakenly "
    "erroneous erroneously misleading misreported disputed"
)
_NUMBER_WORDS = _words(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
    "fifteen sixteen seventeen eighteen nineteen twenty thirty forty fifty sixty seventy "
    "eighty ninety hundred hundreds thousand thousands million millions billion billions "
    "trillion dozen dozens twice thrice double doubled triple tripled quadruple half halved "
    "quarter midnight noon midday"
)
# a word left in a clause beside a bound number that may make it a threshold, a baseline or a
# range (fraud-review-bedrock D-42): the clause's checks are ambiguous
_BLOCK_WORDS = _words(
    "threshold thresholds signal signals limit limits rule rules clause clauses window range "
    "ranges period between from until through respectively cutoff cutoffs ceiling ceilings "
    "policy requirement requirements usual typical median normal average mean baseline norm "
    "trigger triggers bar bars each both"
)
_CLAUSE_WORDS = _words("and but while whereas so which yet because although though since")
_CLAUSE_PUNCT = frozenset(".!?;,()[]—–")
_CURRENCY_WORDS = _words("usd dollars dollar eur euros euro gbp pounds")
_CURRENCY_SIGNS = frozenset("$€£")
_MULTIPLE_WORDS = frozenset({"times", "fold", "x", "×"})
_PERCENT_WORDS = frozenset({"percent", "pct"})
# words that compare; one no binding used makes the reason ambiguous
_CMP_TRIGGERS = _words(
    "above over below under exceed exceeds exceeded exceeding than least most beyond > < >= <= ≥ ≤"
)
# right after a number: a hedge, a range or a list
_AFTER_HEDGES = frozenset(("or", "to", "-", "–", "+", "plus", "ish", "give", "/"))
_SCALE_WORDS = _words("thousand million billion trillion k m bn mn cents cent grand hundred")

_TOKEN = re.compile(
    r"(?P<num>[0-9](?:[0-9,.]*[0-9])?)|(?P<word>[^\W\d_]+(?:'[^\W\d_]+)*)|(?P<sym>>=|<=|\S)"
)
_WELL_FORMED = re.compile(r"[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?|[0-9]+(?:\.[0-9]+)?")
_MAX_DIGITS = 100  # a longer number is malformed (unbound), never parsed
_CLOCK = re.compile(r":([0-5][0-9])(?![0-9])")
_MERIDIEM = re.compile(r"\s?([ap])\.?m(?![a-z])\.?", re.IGNORECASE)


@dataclass
class _Tok:
    kind: str  # num, word, sym, mention
    text: str
    start: int
    end: int
    low: str = ""
    fields: frozenset[str] = frozenset()


@dataclass
class _Num:
    """One number token and how it is written: sign, currency mark, unit, displayed value."""

    tok: int  # index of the number token
    first: int  # index of its first token (sign or currency mark)
    after: int  # index of the first token after it and its unit
    start: int
    end: int  # text end, unit included
    value: Fraction | None  # None when malformed or not finite
    half: Fraction  # half of the last displayed digit
    unit: str | None  # None, pct, mult, cur, time, other
    is_time_minutes: bool = False


@dataclass
class _Bind:
    field: str
    op: str  # "=", ">", ">=", "<", "<="
    via: str  # shape read: left, paren, right, phrase


@dataclass
class _Reading:
    """A number's reading: bound to one field, or unbound with a reason."""

    binds: list[_Bind] = dc_field(default_factory=list)
    void: str | None = None


def _norm_tokens(name: str) -> list[str]:
    return [t for t in re.split(r"[\s_]+", name.replace("’", "'").strip()) if t]


def _mention_spans(
    text: str, record: Mapping[str, Any], aliases: Mapping[str, Sequence[str]]
) -> list[tuple[int, int, frozenset[str]]]:
    """Non-overlapping mentions of fields: a record key's whole name or an alias, case-free,
    spaces and underscores interchangeable, never inside a longer word, plural or hyphenation;
    the longest wins; a name of several fields names all of them (and so binds nothing)."""
    names: dict[str, set[str]] = {}
    for key in record:
        if isinstance(key, str) and (toks := _norm_tokens(key)):
            names.setdefault(" ".join(toks).lower(), set()).add(key)
    for f, phrases in aliases.items():
        for a in phrases:
            if toks := _norm_tokens(a):
                names.setdefault(" ".join(toks).lower(), set()).add(f)
    found: dict[tuple[int, int], set[str]] = {}
    for name, fields in names.items():
        body = r"[\s_]*".join(re.escape(t) for t in name.split(" "))
        pattern = re.compile(rf"(?<![\w\-']){body}(?![\w\-])", re.IGNORECASE)
        for m in pattern.finditer(text):
            if m.end() > m.start():
                found.setdefault((m.start(), m.end()), set()).update(fields)
    spans: list[tuple[int, int, frozenset[str]]] = []
    last = -1
    for (s, e), fields in sorted(found.items(), key=lambda x: (x[0][0], x[0][0] - x[0][1])):
        if s >= last:
            spans.append((s, e, frozenset(fields)))
            last = e
    return spans


def _tokens(text: str, mentions: Sequence[tuple[int, int, frozenset[str]]]) -> list[_Tok]:
    out: list[_Tok] = []
    k = 0
    for m in _TOKEN.finditer(text):
        while k < len(mentions) and mentions[k][1] <= m.start():
            k += 1
        if k < len(mentions) and mentions[k][0] <= m.start() < mentions[k][1]:
            s, e, fields = mentions[k]
            if not out or out[-1].kind != "mention" or out[-1].start != s:
                out.append(_Tok("mention", text[s:e], s, e, text[s:e].lower(), fields))
            continue
        kind = m.lastgroup or "sym"
        out.append(_Tok(kind, m.group(), m.start(), m.end(), m.group().lower()))
    return out


class _Reader:
    """A reason read once: tokens, mentions, numbers and the binding shapes around each."""

    def __init__(
        self,
        text: str,
        record: Mapping[str, Any],
        aliases: Mapping[str, Sequence[str]],
        units: Mapping[str, str],
    ) -> None:
        self.text = text
        self.units = units
        self.toks = _tokens(text, _mention_spans(text, record, aliases))
        self.starts = [t.start for t in self.toks]
        # the words of each field's own key and aliases ("card" for card_history_n)
        self.field_words: dict[str, set[str]] = {}
        for f, names in [*((k, (k,)) for k in record if isinstance(k, str)), *aliases.items()]:
            words = self.field_words.setdefault(f, set())
            for name in names:
                for w in _norm_tokens(name.lower()):
                    words.update((w, w.removesuffix("'s")))

    # -- small helpers
    def word(self, i: int) -> str | None:
        """The lower-case text of token i when it is a word or symbol, else None."""
        if 0 <= i < len(self.toks) and self.toks[i].kind in ("word", "sym"):
            return self.toks[i].low
        return None

    def at(self, pos: int) -> int:
        """Index of the first token starting at or after `pos`."""
        return bisect.bisect_left(self.starts, pos)

    def adjacent(self, i: int, j: int) -> bool:
        """Tokens i and j touch (no space between)."""
        return self.toks[i].end == self.toks[j].start

    def phrase_before(
        self, i: int, phrases: Iterable[tuple[tuple[str, ...], str]]
    ) -> tuple[int, str] | None:
        """The longest listed phrase ending right before token i: (its first index, label)."""
        for words, label in phrases:
            n = len(words)
            if i - n >= 0 and all(self.word(i - n + k) == w for k, w in enumerate(words)):
                return i - n, label
        return None

    def mention_field(self, i: int) -> str | None:
        t = self.toks[i]
        return next(iter(t.fields)) if t.kind == "mention" and len(t.fields) == 1 else None

    def is_mention(self, i: int) -> bool:
        return 0 <= i < len(self.toks) and self.toks[i].kind == "mention"

    def line_start_dash(self, i: int) -> bool:
        t = self.toks[i]
        if t.low not in ("-", "•", "*"):
            return False
        k = t.start - 1
        while k >= 0 and self.text[k] in " \t":
            k -= 1
        return k < 0 or self.text[k] == "\n"

    def clean_before_mention(self, m: int) -> str | None:
        """None when the token before mention m lets it be a subject, else the reason."""
        p = m - 1
        if p >= 0 and self.word(p) in _DETERMINERS:
            p -= 1
        if p < 0:
            return None
        w = self.word(p)
        if w in _COORD and self.is_mention(p - 1):
            return "unclear_field"  # coordinated fields: "risk score and hour are ..."
        if w is not None and (w in _PRE_WORDS or w in _PRE_PUNCT or self.line_start_dash(p)):
            return None
        return "unclear_subject"  # "risk of the amount", "the average amount", "We note amount"

    def clean_after(self, j: int) -> bool:
        """The token at j ends a statement: the end, punctuation or a following word."""
        if j >= len(self.toks) or self.newline_before(j):
            return True
        w = self.word(j)
        if w is None:
            return False
        if w == "." and j + 1 < len(self.toks) and self.adjacent(j, j + 1):
            return self.toks[j + 1].kind != "num"  # ".5" is not an end
        return w in _CLEAN_PUNCT or w in _FOLLOW

    def newline_before(self, j: int) -> bool:
        """A line break between token j - 1 and token j (a list item ends there)."""
        return (
            0 < j < len(self.toks) and "\n" in self.text[self.toks[j - 1].end : self.toks[j].start]
        )

    # -- numbers
    def number(self, i: int) -> _Num:
        t = self.toks[i]
        first = i
        negative = False
        if i > 0 and self.word(i - 1) in ("-", "−") and self.adjacent(i - 1, i):
            at = self.toks[i - 1].start
            if not at or self.text[at - 1] in " \t\n(=:":
                negative = True
                first = i - 1
        cur = False
        if (
            first > 0
            and self.word(first - 1) in _CURRENCY_SIGNS
            and self.adjacent(first - 1, first)
        ):
            cur = True
            first -= 1
            if first > 0 and self.word(first - 1) == "us" and self.adjacent(first - 1, first):
                first -= 1
        elif first > 0 and self.word(first - 1) in ("usd", "eur", "gbp"):
            cur = True
            first -= 1
        value: Fraction | None = None
        half = Fraction(1, 2)
        if len(t.text) <= _MAX_DIGITS and _WELL_FORMED.fullmatch(t.text):
            digits = t.text.replace(",", "")
            value = Fraction(digits)
            if "." in digits:
                half = Fraction(1, 2 * 10 ** len(digits.split(".")[1]))
            if negative:
                value = -value
        # the unit after the number
        pos = t.end
        unit: str | None = "cur" if cur else None
        minutes = False
        if m := _CLOCK.match(self.text, pos):
            unit, minutes, pos = "time", m.group(1) != "00", m.end()
            if mm := _MERIDIEM.match(self.text, pos):
                pos = mm.end()
        elif mm := _MERIDIEM.match(self.text, pos):
            unit, pos = "time", mm.end()
        j = self.at(pos)
        end = pos
        if unit != "time" and j < len(self.toks):
            nxt = self.toks[j]
            touching = nxt.start == pos
            w = nxt.low
            took = 0
            if touching and w == "%":
                unit, took = "pct", 1
            elif touching and nxt.kind == "word":
                unit, took = ("mult" if w == "x" else "other"), 1  # "9x"; "5k", "3rd": other
            elif touching and w == "×":
                unit, took = "mult", 1
            elif touching and w == "-" and self.word(j + 1) == "fold" and self.adjacent(j, j + 1):
                unit, took = "mult", 2
            elif w in _PERCENT_WORDS and nxt.kind == "word":
                unit, took = "pct", 1
            elif w == "per" and self.word(j + 1) == "cent":
                unit, took = "pct", 2
            elif w in _MULTIPLE_WORDS:
                unit, took = "mult", 1
            elif w in _CURRENCY_WORDS and nxt.kind == "word":
                unit, took = "cur", 1
            elif w in _SCALE_WORDS:
                unit, took = "other", 1
            if took:
                j += took
                end = self.toks[j - 1].end
        if value is not None and unit == "time":
            hour = value
            if self.text[t.end : pos].strip(" :0123456789").strip():  # a.m. / p.m.
                pm = "p" in self.text[t.end : pos].lower()
                if hour.denominator != 1 or not 1 <= hour <= 12:
                    value = None
                else:
                    value = Fraction(int(hour) % 12 + (12 if pm else 0))
            elif hour.denominator != 1 or not 0 <= hour <= 23:
                value = None
            half = Fraction(1, 2)
        return _Num(i, first, j, self.toks[first].start, end, value, half, unit, minutes)

    def comparison_before(self, i: int) -> tuple[int, str] | None:
        return self.phrase_before(i, _CMP_PHRASES)

    def left(self, n: _Num) -> tuple[str | None, str, int, str | None]:
        """The field named before the number: (field or None, op, mention index, void)."""
        i = n.first
        if i == 0 or self.newline_before(i):
            return None, "=", -1, None
        w = self.word(i - 1)
        m = -1
        op = "="
        if self.is_mention(i - 1):
            m = i - 1
        elif w == "(" and self.is_mention(i - 2):
            m = i - 2
            op = "("
        elif w in ("=", ":") and self.is_mention(i - 2):
            m = i - 2
        elif cmp := self.comparison_before(i):
            k, op = cmp
            if self.word(k - 1) in ("is", "was"):
                k -= 1
            if self.is_mention(k - 1):
                m = k - 1
        elif eq := self.phrase_before(i, ((p, "=") for p in _EQ_PHRASES)):
            k, _ = eq
            if self.is_mention(k - 1):
                m = k - 1
        if m < 0:
            return None, op, -1, None
        f = self.mention_field(m)
        if f is None:
            return None, op, m, "unclear_field"
        why = self.clean_before_mention(m)
        return f, op, m, why

    def right(self, n: _Num, j: int) -> tuple[str | None, int, str | None]:
        """The field named right after the number ("40 earlier purchases"): (field, mention
        index, void)."""
        if not self.is_mention(j) or self.newline_before(j):
            return None, -1, None
        f = self.mention_field(j)
        if f is None:
            return None, j, "unclear_field"
        if not self.clean_after(j + 1):
            return None, j, "unclear_subject"
        return f, j, None

    def unit_ok(self, unit: str | None, f: str, phrase: bool) -> bool:
        declared = self.units.get(f)
        if unit is None:
            return True
        if unit == "pct":
            return declared in ("fraction", "percent")
        if unit == "mult":
            return phrase or declared == "multiple"
        if unit == "cur":
            return phrase or declared == "currency"
        if unit == "time":
            return declared == "hour"
        return False

    def read(self, n: _Num, phrase_binds: list[_Bind], phrase_void: str | None) -> _Reading:
        """Bind number n to one field, or say why it is unbound."""
        if n.value is None:
            return _Reading(void="malformed" if n.unit != "time" else "time")
        if n.unit == "time" and n.is_time_minutes:
            return _Reading(void="time")
        before = self.word(n.first - 1)
        j = n.after
        lf, lop, lm, lvoid = self.left(n)
        rf, rm, rvoid = self.right(n, j) if lm < 0 else (None, -1, None)
        structural: _Bind | None = None
        void: str | None = None
        if lm >= 0 and lop == "(":
            # "<field> (<number>)": the closing bracket must follow
            if self.word(j) != ")":
                void = "unclear_subject"
            elif lvoid or lf is None:
                void = lvoid or "unclear_field"
            else:
                structural = _Bind(lf, "=", "paren")
        elif lm >= 0:
            if lvoid or lf is None:
                void = lvoid or "unclear_field"
            elif self.is_mention(j):
                void = "unclear_field"  # a field on both sides
            elif not self.clean_after(j):
                void = "unclear_subject"
            else:
                structural = _Bind(lf, lop, "left")
        elif rf is not None or rvoid:
            cmp = self.comparison_before(n.first)
            k = cmp[0] if cmp else n.first  # the first token of "[<cmp>] <n>"
            if rvoid:
                void = rvoid
            elif cmp and before == "over":
                void = "unclear_comparison"  # "over 517 earlier purchases": across, or above?
            elif self.subject_ok(k, rf or ""):
                structural = _Bind(rf or "", cmp[1] if cmp else "=", "right")
            else:
                void = "unclear_subject"  # "Shipping was 108 days", "The merchant has 40 ..."
        if structural is not None and not self.unit_ok(n.unit, structural.field, False):
            structural, void = None, "unit"
        if phrase_void:
            return _Reading(void=phrase_void)
        # a phrase's number with another field named right before or after it binds nothing
        # ("Risk score is 9.1x usual", "The risk score (9.1x usual)": verify516)
        attached = [self.toks[m].fields for m in (lm, rm) if m >= 0]
        if phrase_binds and any(fs != {b.field} for fs in attached for b in phrase_binds):
            return _Reading(void="unclear_field")
        binds = [b for b in phrase_binds if self.unit_ok(n.unit, b.field, True)]
        if phrase_binds and not binds:
            return _Reading(void="unit")
        if structural is not None:
            binds.append(structural)
        elif binds and void not in (None, "unit", "unclear_subject"):
            return _Reading(void=void)
        if not binds:
            if void == "unit" and n.unit == "time":
                return _Reading(void="time")
            if void:
                return _Reading(void=void)
            return _Reading(void="time" if n.unit == "time" else "no_field")
        if len({(b.field, b.op) for b in binds}) > 1:
            return _Reading(void="unclear_field")
        return _Reading(binds=binds[:1])

    def subject_ok(self, k: int, f: str) -> bool:
        """Before "[<cmp>] <n> <field>" starting at token k: the start, a sentence end, a line
        break, a joining word, or "<subject> has|had|have" whose subject is a word of the
        field's own name, itself at the start of its phrase ("The card has ...")."""
        if k == 0 or self.newline_before(k):
            return True
        before = self.word(k - 1)
        if before in _SENTENCE_END or before in _NUM_PRE_WORDS:
            return True  # never after a comma, colon, dash or bracket: "Amount, 40 card history"
        subject = k - 2
        return (
            before in _HAS
            and subject >= 0
            and self.toks[subject].kind == "word"
            and self.toks[subject].low in self.field_words.get(f, set())
            and self.clean_before_mention(subject) is None
        )

    def phrase_op(self, n: _Num) -> tuple[str | None, str | None]:
        """For a number inside a field phrase: (comparison, void)."""
        cmp = self.comparison_before(n.first)
        before = self.word(n.first - 1)
        if self.word(n.after) in _AFTER_HEDGES:
            return None, "hedged"  # "$812 or so", "9-10x", "9x/10x", "9 to 10 times"
        if cmp:
            return cmp[1], None
        if before in _HEDGES:
            return None, "hedged"
        return "=", None

    def comparison_tokens(self, n: _Num) -> list[int]:
        """The comparison tokens right before a bound number n."""
        cmp = self.comparison_before(n.first)
        return list(range(cmp[0], n.first)) if cmp else []

    def stray_comparison(self, used: set[int]) -> bool:
        """A comparison word no binding used ("$812, or less than that", "41 or more", "a
        signal under P6.1")."""
        for i, t in enumerate(self.toks):
            if i in used or t.kind not in ("word", "sym"):
                continue
            w = t.low
            if w in _CMP_TRIGGERS or (w in _POSTFIX and self.word(i - 1) in ("or", "and")):
                return True
        return False


def _settle_number(
    f: str,
    op: str,
    claim: Fraction,
    half: Fraction,
    unit: str | None,
    record: Mapping[str, Any],
    units: Mapping[str, str],
    tolerance: float,
) -> tuple[str, str | None, Any]:
    """(status, reason, value found) for one bound number."""
    if f not in record or _missing(record[f]):
        return "unchecked", f"missing_value:{f}", None
    raw = record[f]
    if _type(raw) != "number":
        return "unchecked", f"type_error:{f}", _plain(raw)
    try:
        v = Fraction(repr(raw)) if isinstance(raw, float) else Fraction(raw)  # 0.83 is 0.83
    except (OverflowError, TypeError, ValueError):
        return "unchecked", f"type_error:{f}", _plain(raw)
    if unit == "pct" and units.get(f) == "fraction":
        claim, half = claim / 100, half / 100
    elif units.get(f) == "fraction" and abs(claim) > 1:
        return "unchecked", f"unit:{f}", _plain(raw)  # "3.8" on a share: a percent without %?
    if op != "=":
        holds = {">": v > claim, ">=": v >= claim, "<": v < claim, "<=": v <= claim}[op]
        return ("ok" if holds else "failed"), None, raw
    diff = abs(claim - v)
    if isinstance(raw, int):  # integer fields are exact (D-74)
        return ("ok" if diff == 0 else "failed"), None, raw
    if tolerance and diff <= Fraction(tolerance) * abs(v):
        return "ok", None, raw
    if diff > 2 * half * (1 + Fraction(1, 10**9)):
        return "failed", None, raw  # off by more than one unit of the last digit shown
    if diff > half * (1 + Fraction(1, 10**9)):
        if diff > _COARSE * abs(v):
            return "failed", None, raw
        return "ambiguous", "rounding", raw  # one unit off: truncated, not clearly false
    if diff <= _COARSE * abs(v) and diff <= max(_ABS_TOL, _REL_TOL * abs(v)):
        return "ok", None, raw
    return "ambiguous", "coarse_rounding", raw  # a correct rounding, too coarse to verify


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


def _cover(length: int, spans: Iterable[tuple[int, int]]) -> bytearray:
    out = bytearray(length + 1)
    for s, e in spans:
        out[s:e] = b"\x01" * (e - s)
    return out


def _check_reason(
    reason: str,
    record: Mapping[str, Any],
    phrases: Sequence[ValuePhrase],
    tolerance: float,
    *,
    aliases: Mapping[str, Sequence[str]],
    units: Mapping[str, str],
    constants: Sequence[float] = (),
    ignore: Sequence[re.Pattern[str]] = (),
    unreadable: Sequence[re.Pattern[str]] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(checks, unchecked) for one reason; `unchecked` lists the numbers inside `ignore`
    spans (ids, never claims)."""
    text = reason.replace("’", "'").replace("‘", "'")  # same length: positions quote `reason`
    rd = _Reader(text, record, aliases, units)
    toks = rd.toks
    ignored = _cover(
        len(text),
        ((m.start(), m.end()) for p in ignore for m in p.finditer(text) if m.end() > m.start()),
    )
    checks: list[dict[str, Any]] = []
    unchecked: list[dict[str, Any]] = []

    def quote(s: int, e: int) -> str:
        return reason[s:e].strip()

    # numbers: not ids glued to letters ("P7.1", "x2", "COVID-19"), not in an ignored span
    nums: list[_Num] = []
    id_tokens: list[int] = []
    last_end = 0
    for i, t in enumerate(toks):
        if t.kind != "num" or t.start < last_end:
            continue  # not a number, or the minutes of a clock time read already
        prev = text[t.start - 1] if t.start else " "
        if (
            prev.isalpha()
            or prev == "_"
            or (prev == "-" and t.start >= 2 and text[t.start - 2].isalpha())
        ):
            id_tokens.append(i)
            continue
        n = rd.number(i)
        last_end = n.end
        if ignored[t.start]:
            unchecked.append({"text": quote(n.start, n.end), "kind": "ignored"})
            continue
        nums.append(n)
    # field phrases: the one number inside each match is bound to the phrase's field
    phrase_binds: dict[int, list[_Bind]] = {}
    phrase_void: dict[int, str] = {}
    num_starts = [toks[n.tok].start for n in nums]
    phrase_spans: list[tuple[int, int]] = []
    for ph in phrases:
        if ph.field is None:
            continue
        for m in re.finditer(ph.pattern, text, re.IGNORECASE):
            if m.end() == m.start():
                continue
            phrase_spans.append((m.start(), m.end()))
            lo = bisect.bisect_left(num_starts, m.start())
            hi = bisect.bisect_left(num_starts, m.end())
            inside = [n for n in nums[lo:hi] if toks[n.tok].start < m.end()]
            if len(inside) > 1:
                for n in inside:
                    phrase_void[n.tok] = "unclear_field"
                continue
            for n in inside:
                op, void = rd.phrase_op(n)
                if void:
                    phrase_void[n.tok] = void
                else:
                    phrase_binds.setdefault(n.tok, []).append(_Bind(ph.field, op or "=", "phrase"))
    # each number's reading; a field named for two numbers binds neither
    readings = {
        n.tok: rd.read(n, phrase_binds.get(n.tok, []), phrase_void.get(n.tok)) for n in nums
    }
    # a phrase's or a right-hand field's number binds nothing when another field is named in
    # its clause ("The hour is 5% of the card's purchases", "Amount of under 0.9 risk score")
    clause = _clauses(rd)
    named: dict[int, list[frozenset[str]]] = {}
    for i, t in enumerate(toks):
        if t.kind == "mention":
            named.setdefault(clause[i], []).append(t.fields)
    for n in nums:
        r = readings[n.tok]
        if r.binds and r.binds[0].via in ("phrase", "right"):
            f = r.binds[0].field
            if any(fs != {f} for fs in named.get(clause[n.tok], ())):
                readings[n.tok] = _Reading(void="unclear_field")
    used_cmp: set[int] = set()
    for n in nums:
        if readings[n.tok].binds:
            used_cmp.update(rd.comparison_tokens(n))
    consts = {Fraction(c) for c in constants}
    for n in nums:
        r = readings[n.tok]
        base: dict[str, Any] = {"kind": "number", "text": quote(n.start, n.end), "start": n.start}
        used_cmp.update(range(n.first, n.after))  # its own tokens are not stray words
        if r.void or not r.binds:
            why = r.void or "no_field"
            if n.value is not None and n.value in consts and why == "no_field":
                why = "constant"
            checks.append(
                {
                    **base,
                    "field": None,
                    "op": None,
                    "claimed": _float(n.value),
                    "found": None,
                    "ok": False,
                    "status": "unbound",
                    "reason": why,
                }
            )
            continue
        b = r.binds[0]
        assert n.value is not None
        status, why, found = _settle_number(
            b.field, b.op, n.value, n.half, n.unit, record, units, tolerance
        )
        checks.append(
            {
                **base,
                "field": b.field,
                "op": b.op,
                "claimed": _float(n.value),
                "found": found,
                "ok": status == "ok",
                "status": status,
                "reason": why,
            }
        )
    # numbers written in words or other scripts are never bound
    consumed: set[int] = set()
    for i, t in enumerate(toks):
        if t.kind == "word" and t.low in ("true", "false"):
            got = _bool_claim(rd, i, record)
            if got is not None:
                consumed.add(i)
                checks.append(got)
            else:  # a yes/no claim the text does not bind is never dropped
                checks.append(_unbound(quote(t.start, t.end), t.start, "unclear_subject"))
        elif t.kind == "word" and t.low in _NUMBER_WORDS and not ignored[t.start]:
            checks.append(_unbound(quote(t.start, t.end), t.start, "number_word"))
        elif t.kind in ("word", "sym") and any(
            (ch.isnumeric() and not ch.isascii()) or ch == "∞" for ch in t.text
        ):
            checks.append(_unbound(quote(t.start, t.end), t.start, "non_ascii_number"))
    # condition phrases
    for ph in phrases:
        if ph.holds is None:
            continue
        condition: tuple[bool, dict[str, Any], str | None] | None = None
        names = _names(parse_predicate(ph.holds, None, f"phrase {ph.pattern!r}"))
        for m in re.finditer(ph.pattern, text, re.IGNORECASE):
            if m.end() == m.start():
                continue
            phrase_spans.append((m.start(), m.end()))
            if condition is None:  # the record does not change: evaluate once
                try:
                    condition = _condition(ph.holds, record, f"phrase {ph.pattern!r}")
                except (RuleError, OverflowError, TypeError, ValueError):
                    # the grammar was checked when the ValuePhrase was built: an error here
                    # comes from the record's values, so the claim is unchecked, never raised
                    condition = (False, {}, f"type_error:{ph.holds}")
            ok, found, why = condition
            found = {k: _plain(v) for k, v in found.items()}
            status = "unchecked" if why else ("ok" if ok else "failed")
            checks.append(
                {
                    "kind": "phrase",
                    "text": quote(m.start(), m.end()),
                    "start": m.start(),
                    "_fields": names,
                    "holds": ph.holds,
                    "found": found,
                    "ok": status == "ok",
                    "status": status,
                    "reason": why,
                }
            )
    # a clause (between sentence ends, commas, brackets, dashes and joining words) that binds
    # two fields, holds a rule id, or keeps a threshold, baseline or range word beside its
    # claims is ambiguous: "amount 11.01 is 0.21 times the card's usual", "Amount is 11.01
    # under P3.1", "risk score of 0.83 against the 0.9 threshold"
    covered = _cover(len(text), phrase_spans)
    flags = _clause_flags(rd, clause, checks, id_tokens, covered)
    for c in checks:
        why = flags.get(clause[max(0, bisect.bisect_right(rd.starts, c["start"]) - 1)])
        if why and c["status"] in ("ok", "failed"):
            c.update(ok=False, status="ambiguous", reason=why)
    # a negation, hypothetical, denial, "respectively", question, comparison word no binding
    # used, or a declared unreadable claim anywhere voids every check of the reason
    void = (
        _reason_void(rd, consumed)
        or ("unclear_comparison" if rd.stray_comparison(used_cmp) else None)
        or (
            "unreadable"
            if any(not covered[m.start()] for p in unreadable for m in p.finditer(text))
            else None
        )
    )
    if void:
        for c in checks:
            if c["status"] in ("ok", "failed"):
                c.update(ok=False, status="ambiguous", reason=void)
    checks.sort(key=lambda c: c["start"])
    for c in checks:
        del c["start"]
        c.pop("_fields", None)
    return checks, unchecked


def _clauses(rd: _Reader) -> list[int]:
    """The clause number of each token."""
    out: list[int] = []
    k = 0
    for i, t in enumerate(rd.toks):
        w = t.low
        if (
            (t.kind == "sym" and w in _CLAUSE_PUNCT)
            or (t.kind == "word" and w in _CLAUSE_WORDS)
            or rd.newline_before(i)
        ):
            k += 1
        out.append(k)
    return out


def _clause_flags(
    rd: _Reader,
    clause: Sequence[int],
    checks: Sequence[Mapping[str, Any]],
    id_tokens: Sequence[int],
    covered: bytearray,
) -> dict[int, str]:
    """Clauses whose checks are ambiguous, with the reason."""
    fields: dict[int, set[str]] = {}
    for c in checks:
        if c["status"] in ("ok", "failed", "unchecked"):
            k = clause[max(0, bisect.bisect_right(rd.starts, c["start"]) - 1)]
            got = c.get("_fields") or ((c["field"],) if c.get("field") else ())
            fields.setdefault(k, set()).update(got)
    flags = {k: "unclear_field" for k, f in fields.items() if len(f) > 1}
    for i in id_tokens:
        if clause[i] in fields:
            flags.setdefault(clause[i], "rule_id")
    for i, t in enumerate(rd.toks):
        k = clause[i]
        if k not in fields or k in flags or covered[t.start]:
            continue
        if (t.kind == "word" and (t.low in _BLOCK_WORDS or t.low.startswith("criteri"))) or (
            t.kind == "sym" and t.low == "+"
        ):
            flags[k] = "block_word"
    return flags


def _plain(v: Any) -> Any:
    """A record value as shown in details: a JSON scalar, else None."""
    if isinstance(v, bool | int | str) or (isinstance(v, float) and math.isfinite(v)):
        return v
    return None


def _float(v: Fraction | None) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except OverflowError:
        return None


def _unbound(text: str, start: int, why: str) -> dict[str, Any]:
    return {
        "kind": "number",
        "text": text,
        "start": start,
        "field": None,
        "op": None,
        "claimed": None,
        "found": None,
        "ok": False,
        "status": "unbound",
        "reason": why,
    }


def _bool_claim(rd: _Reader, i: int, record: Mapping[str, Any]) -> dict[str, Any] | None:
    """ "<field> is true", "<field>: false", "(<field> true)": a check, or None when "true" or
    "false" is not bound to a field this way."""
    w = rd.word(i - 1)
    m = i - 1 if rd.is_mention(i - 1) else -1
    if m < 0 and w in ("is", "was", "=", ":") and rd.is_mention(i - 2):
        m = i - 2
    if m < 0 or not rd.clean_after(i + 1):
        return None
    f = rd.mention_field(m)
    if f is None or rd.clean_before_mention(m):
        return None
    t = rd.toks[i]
    claimed = t.low == "true"
    base = {
        "kind": "bool",
        "text": rd.text[rd.toks[m].start : t.end],
        "start": rd.toks[m].start,
        "field": f,
        "op": "=",
        "claimed": claimed,
    }
    if f not in record or _missing(record[f]):
        return {
            **base,
            "found": None,
            "ok": False,
            "status": "unchecked",
            "reason": f"missing_value:{f}",
        }
    found = record[f]
    if not isinstance(found, bool):
        return {
            **base,
            "found": _plain(found),
            "ok": False,
            "status": "unchecked",
            "reason": f"type_error:{f}",
        }
    ok = found is claimed
    return {**base, "found": found, "ok": ok, "status": "ok" if ok else "failed", "reason": None}


def _reason_void(rd: _Reader, consumed: set[int]) -> str | None:
    for i, t in enumerate(rd.toks):
        if t.kind == "sym" and t.text == "?":
            return "question"
        if t.kind == "sym" and t.text in ("~", "≈"):
            return "hedged"
        if t.kind != "word":
            continue
        w = t.low
        if w in _NEGATIONS or w.endswith("n't"):
            return "negation"
        if w in _HYPOTHETICAL or w.startswith(_HYPOTHETICAL_STEMS):
            return "hypothetical"
        if w in _DENIALS and i not in consumed:
            return "denial"
        if w == "respectively":
            return "respectively"
        if w in _HEDGE_WORDS or any(
            all(rd.word(i + k) == x for k, x in enumerate(p)) for p in _HEDGE_PHRASES
        ):
            return "hedged"
    return None


def _outcome(checks: Sequence[Mapping[str, Any]]) -> Outcome:
    """D-72: contradicted when any check failed; verified when there is at least one check and
    every one is ok; else unverifiable."""
    statuses = {c["status"] for c in checks}
    if "failed" in statuses:
        return "contradicted"
    return "verified" if statuses == {"ok"} else "unverifiable"


def _options(
    aliases: Mapping[str, Sequence[str]] | None,
    units: Mapping[str, str] | None,
    constants: Sequence[float],
    ignore: Sequence[str],
    unreadable: Sequence[str] = (),
) -> tuple[
    dict[str, tuple[str, ...]],
    dict[str, str],
    tuple[float, ...],
    tuple[re.Pattern[str], ...],
    tuple[re.Pattern[str], ...],
]:
    """Validate the caller's options (a bad option is the caller's error: ValueError)."""
    fixed_aliases: dict[str, tuple[str, ...]] = {}
    for f, names in (aliases or {}).items():
        if not isinstance(f, str) or isinstance(names, str):
            raise ValueError("aliases maps a field name to a list of phrases")
        if not all(isinstance(a, str) and a.strip() for a in names):
            raise ValueError(f"aliases[{f!r}]: every phrase is non-blank text")
        fixed_aliases[f] = tuple(names)
    fixed_units: dict[str, str] = {}
    for f, kind in (units or {}).items():
        if not isinstance(f, str) or kind not in UNITS:
            raise ValueError(f"units maps a field name to one of {', '.join(UNITS)}")
        fixed_units[f] = kind
    if isinstance(constants, str | bytes):
        raise ValueError("constants is a list of numbers")
    fixed_constants: list[float] = []
    for c in constants:
        if isinstance(c, bool) or not isinstance(c, int | float) or not math.isfinite(c):
            raise ValueError(f"constant {c!r} is not a finite number")
        fixed_constants.append(float(c))
    compiled: list[tuple[re.Pattern[str], ...]] = []
    for name, regexes in (("ignore", ignore), ("unreadable", unreadable)):
        if isinstance(regexes, str):
            raise ValueError(f"{name} is a list of regexes, not one string")
        patterns: list[re.Pattern[str]] = []
        for p in regexes:
            try:
                patterns.append(re.compile(p, re.IGNORECASE))
            except (re.error, TypeError) as exc:
                raise ValueError(f"{name} pattern {p!r} is not a regex: {exc}") from exc
        compiled.append(tuple(patterns))
    return fixed_aliases, fixed_units, tuple(fixed_constants), compiled[0], compiled[1]


def value_check(
    reason: str,
    record: Mapping[str, Any],
    *,
    phrases: Sequence[ValuePhrase] = (),
    tolerance: float = 0.0,
    aliases: Mapping[str, Sequence[str]] | None = None,
    units: Mapping[str, Unit] | None = None,
    constants: Sequence[float] = (),
    ignore: Sequence[str] = (),
    unreadable: Sequence[str] = (),
) -> ScoreResult:
    """Check the numbers and declared phrases in `reason` against `record`, strictly (D-74).

    A number is checked only when the text binds it to one field: the field's whole key or an
    alias from `aliases` (case-free; spaces and underscores interchangeable) in one of these
    shapes, nothing else between: "<field> is|was|of|at|=|:|equals|totals <n>", "<field> (<n>)",
    "<field> <n>", "<field> [is|was] above|over|below|under|exceeds|more than|less than|at
    least|at most|>|<|>=|<= <n>", "[<comparison>] <n> <field>", or the one number inside a
    `ValuePhrase(field=...)` match. The word before a field named first is the start,
    punctuation, a determiner or a joining word (and, with, at, ...); the word after the
    number (or after a field named after it) is the end, punctuation, a line break or one of
    and, but, while, whereas, so, which, is, was, are, were. "[<comparison>] <n> <field>"
    follows the start, a sentence end, a line break, a joining word, or "<subject> has" whose
    subject is a word of that field's own name ("The card has 40 earlier purchases", never
    "The merchant has ..." or "Shipping was 108 days"). A phrase's or a right-hand field's
    number binds nothing when another field is named right before it or in its clause ("Risk
    score is 9.1x usual"). Fields joined by and, or, /, &, a field on both sides, or two
    fields for one number bind nothing. No number is ever matched
    to a field by value. Every other number (an unknown subject, a partial or plural name, a
    list, a range, "about 9", "41 or more", a time with no hour field, a constant, a number
    word) is `unbound`, so its reason is unverifiable.

    Units must be declared per field in `units`: "fraction" (field 0-1; "83%" is 0.83, a bare
    number above 1 is unchecked), "percent" (field 0-100), "multiple" ("9x", "9 times"),
    "currency" ("$812.40", "812 USD"), "hour" (field 0-23; "3 a.m.", "15:00"; other minutes
    are unbound). An undeclared unit, a scale ("5k", "thousand", "cents") or a suffix ("3rd",
    "1e9") is unbound; a phrase-bound number may carry a multiple or currency mark. Integer
    fields are exact. A non-integer field equals a claim when it rounds to the claim at the
    claim's displayed precision and is within 0.005 or 0.5% of it (and at most 10% off);
    a coarser correct rounding is `ambiguous` (`coarse_rounding`, `rounding`), never a
    failure. `tolerance` (relative, non-integer fields only) widens this. Comparisons are
    exact. "true" / "false" bound to a boolean field the same way are checked. Condition
    phrases (`ValuePhrase(holds=...)`) are checked with the rule grammar.

    A clause (between sentence ends, commas, brackets, dashes and joining words) that binds two
    fields, holds a rule id ("P3.1"), or keeps a threshold, baseline or range word ("limit",
    "usual", "between", ...) outside every phrase match makes its checks `ambiguous`. A
    negation (not, no, never, n't, without, ...), a hypothetical, condition or time-shift word
    (if, would, requires, when, eligible, expected, previously, ...), a denial (false, untrue,
    wrong, ...), "respectively", a "?", a comparison word no binding used, or a match of an
    `unreadable` regex outside every phrase match (claims the caller knows this check cannot
    read, such as "rare"), or a hedge (roughly, about, approximately, around, nearly, almost,
    ballpark, circa, ~, give or take, more or less, or so) anywhere in the reason makes every
    check `ambiguous`: it neither verifies nor contradicts. A "true" or "false" the text does
    not bind to a boolean field is an unbound claim, never dropped. Numbers inside `ignore`
    spans are ids, listed in `unchecked` and never claims; negations there are still read.
    `constants` only label an unbound number (`reason` "constant").

    Outcome (D-72), in `details["outcome"]`: `contradicted` when any check failed; `verified`
    when there is at least one check and every check is ok; else `unverifiable`; the value is
    1.0 only when verified. Each check has `status`: ok, failed, unbound (a number not bound;
    `reason` no_field, unclear_subject, unclear_field, unit, hedged, time, malformed,
    constant, number_word, non_ascii_number), ambiguous (negation, hypothetical, denial,
    respectively, question, hedged, unclear_comparison, unreadable, unclear_field, rule_id,
    block_word, rounding, coarse_rounding) or unchecked (`missing_value:<field>`,
    `type_error:<field>`, `unit:<field>`). Details also hold `failed`, `unverified` (the
    text of every check neither ok nor failed), `n_checked` and `unchecked`."""
    if not 0 <= tolerance < 1:
        raise ValueError("tolerance must be in [0, 1)")
    al, un, consts, patterns, unread = _options(aliases, units, constants, ignore, unreadable)
    checks, unchecked = _check_reason(
        reason,
        record,
        phrases,
        tolerance,
        aliases=al,
        units=un,
        constants=consts,
        ignore=patterns,
        unreadable=unread,
    )
    return _result(checks, unchecked)


def _result(checks: list[dict[str, Any]], unchecked: list[dict[str, Any]]) -> ScoreResult:
    outcome = _outcome(checks)
    return ScoreResult(
        value=1.0 if outcome == "verified" else 0.0,
        details={
            "outcome": outcome,
            "checks": checks,
            "failed": [c["text"] for c in checks if c["status"] == "failed"],
            "unverified": [c["text"] for c in checks if c["status"] not in ("ok", "failed")],
            "n_checked": len(checks),
            "unchecked": unchecked,
        },
    )


def value_check_scorer(
    *,
    phrases: Sequence[ValuePhrase] = (),
    name: str = "evidence_values",
    record_key: str = "record",
    tolerance: float = 0.0,
    aliases: Mapping[str, Sequence[str]] | None = None,
    units: Mapping[str, Unit] | None = None,
    constants: Sequence[float] = (),
    ignore: Sequence[str] = (),
    unreadable: Sequence[str] = (),
    version: str = "2",
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
    al, un, consts, patterns, unread = _options(aliases, units, constants, ignore, unreadable)

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
                reason,
                record,
                fixed,
                tolerance,
                aliases=al,
                units=un,
                constants=consts,
                ignore=patterns,
                unreadable=unread,
            )
            checks += [{**c, "reason_index": k} for c in got]
            unchecked += [{**c, "reason_index": k} for c in skipped]
            per_reason.append({"index": k, "outcome": _outcome(got), "n_checked": len(got)})
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
