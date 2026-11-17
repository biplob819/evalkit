"""Structured evidence (SEV-01): judge a decision's reasons against the tabular record it was
made on.

Two parts, built for fraud-review-bedrock (Tech Spec §5.5 and EVK-02, MIT licence,
Copyright (c) 2026 Biplob Chakraborty, the same author) and general to any decision system:

- `render_evidence` turns a record into numbered evidence sentences, so `faithfulness()` can
  judge each reason against the record exactly as it judges an explanation against a document
  (sentence i of the list is evidence sentence i; the judge prompt numbers them).
- `value_check` checks the numbers and comparisons in a reason against the record without a
  model: phrases a project declares ("9x usual spend" -> `amt_vs_usual`, "at night" ->
  `hour < 5`) and every other number, within the displayed precision or a relative tolerance.

Templates read plain field names only; conditions use the RUL-01 predicate grammar. Nothing
from a record, template, phrase or reason is ever executed.
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Mapping, Sequence
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
_COMPARATOR = re.compile(
    # a negation, possibly with up to two words before the comparison ("not, in fact, above",
    # "no purchase above", "cannot exceed", "isn‘t above")
    r"(?P<neg>(?:\b(?:not|no|never|cannot|nothing|without)\b|n['’‘]t\b)[\s,]+"
    r"(?:[a-z]+[\s,]+){0,2}?)?"
    r"(?P<cmp>less than or equal to|greater than or equal to|more than or equal to|"
    r"at or below|at or above|more than|greater than|higher than|fewer than|less than|"
    r"lower than|at least|at most|up to|exceeding|exceeds|exceed|above|below|under|over|"
    r">=|<=|≥|≤|>|<)\s*[$£€]?\s*$",
    re.IGNORECASE,
)
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
}
_NEGATE = {">": "<=", ">=": "<", "<": ">=", "<=": ">"}
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


def _check_reason(
    reason: str, record: Mapping[str, Any], phrases: Sequence[ValuePhrase], tolerance: float
) -> list[dict[str, Any]]:
    numbers = extract_numbers(reason)
    starts = [n.start for n in numbers]  # extract_numbers returns reading order
    used: set[int] = set()
    checks: list[dict[str, Any]] = []
    for ph in phrases:
        where = f"phrase {ph.pattern!r}"
        if ph.holds is not None:
            condition: tuple[bool, dict[str, Any], str | None] | None = None
            for m in re.finditer(ph.pattern, reason, re.IGNORECASE):
                if condition is None:  # the record does not change: evaluate once
                    try:
                        condition = _condition(ph.holds, record, where)
                    except (RuleError, OverflowError, TypeError, ValueError):
                        # the phrase's grammar was checked when the ValuePhrase was built, so an
                        # error here comes from the record's values: fail this claim, never raise
                        condition = (False, {}, f"type_error:{ph.holds}")
                ok, found, why = condition
                check = {
                    "text": m.group(0),
                    "kind": "condition",
                    "holds": ph.holds,
                    "found": found,
                    "ok": ok,
                }
                checks.append({**check, "reason": why} if why else check)
            continue
        assert ph.field is not None
        for m in re.finditer(ph.pattern, reason, re.IGNORECASE):
            i = bisect.bisect_left(starts, m.start())
            while i < len(numbers) and starts[i] < m.end() and i in used:
                i += 1
            if not (i < len(numbers) and starts[i] < m.end()):
                continue  # the phrase states no number here
            used.add(i)
            n = numbers[i]
            op = _comparison(reason, n)
            try:
                value = _numeric(record, ph.field, where)
            except RuleError:  # a field of the wrong type fails this claim; never raises
                checks.append(
                    {
                        "text": m.group(0),
                        "kind": "value",
                        "field": ph.field,
                        "op": op,
                        "claimed": _claimed(n),
                        "found": None,
                        "ok": False,
                        "reason": f"type_error:{ph.field}",
                    }
                )
                continue
            ok = value is not None and _finite(n) and _claim(n, op, value, tolerance)
            check = {
                "text": m.group(0),
                "kind": "value",
                "field": ph.field,
                "op": op,
                "claimed": _claimed(n),
                "found": value,
                "ok": ok,
            }
            checks.append(
                {**check, "reason": f"missing_value:{ph.field}"} if value is None else check
            )
    numeric = {k: f for k, v in record.items() if (f := _as_float(v)) is not None}
    for i, n in enumerate(numbers):
        if i in used:
            continue
        # the field the number names: the first within tolerance; a comparison word before
        # the number ("above 900") then checks that comparison against it
        match = next((k for k, v in numeric.items() if _close(n, v, tolerance)), None)
        op = _comparison(reason, n)
        ok = _finite(n) and match is not None and _claim(n, op, numeric[match], tolerance)
        checks.append(
            {
                "text": n.text,
                "kind": "number",
                "field": match,
                "op": op,
                "claimed": _claimed(n),
                "found": numeric[match] if match else None,
                "ok": ok,
            }
        )
    return checks


def _comparison(reason: str, n: Number) -> str:
    """The comparison stated around the number, else "≈" (equal): a word right before it
    ("above 900"), inverted by a negation ("not above 900", "doesn't exceed 900"), or a
    phrase right after it ("900 or less")."""
    cmp = _COMPARATOR.search(reason[max(0, n.start - _COMPARATOR_WINDOW) : n.start])
    if cmp:
        op = _OP[cmp.group("cmp").lower()]
        return _NEGATE[op] if cmp.group("neg") else op
    after = reason[n.start + len(n.text) : n.start + len(n.text) + _COMPARATOR_WINDOW]
    post = _POSTFIX.match(after)
    return _POSTFIX_OP[post.group("word").lower()] if post else "≈"


def _claimed(n: Number) -> float | None:
    if not _finite(n):
        return None
    return -n.value if n.negative else n.value


def value_check(
    reason: str,
    record: Mapping[str, Any],
    *,
    phrases: Sequence[ValuePhrase] = (),
    tolerance: float = 0.1,
) -> ScoreResult:
    """Check every number and declared phrase in `reason` against `record`. 1.0 when nothing
    fails (also when nothing was checkable), else 0.0. Details: `checks`, `failed` (the text of
    each failed check), `n_checked`.

    A number written with a minus must match a negative value; an unsigned number ("fell
    0.83") is compared with the value's magnitude. A number after a comparison word ("above
    900", "at least 3") is checked as that comparison against the field it names (the first
    within tolerance). A field the record lacks, or a missing value, fails the claim that needs
    it, with `reason` `missing_value:<field>` on the check."""
    if not 0 <= tolerance < 1:
        raise ValueError("tolerance must be in [0, 1)")
    checks = _check_reason(reason, record, phrases, tolerance)
    return _result(checks)


def _result(checks: list[dict[str, Any]]) -> ScoreResult:
    failed = [c["text"] for c in checks if not c["ok"]]
    return ScoreResult(
        value=0.0 if failed else 1.0,
        details={"checks": checks, "failed": failed, "n_checked": len(checks)},
    )


def value_check_scorer(
    *,
    phrases: Sequence[ValuePhrase] = (),
    name: str = "evidence_values",
    record_key: str = "record",
    tolerance: float = 0.1,
    version: str = "1",
) -> Scorer:
    """Register a task scorer that runs `value_check` on each record: the record from
    `case.meta[record_key]`, each string in the `reasons` list of a JSON-object answer (else
    the answer text). A failed call scores 0.0."""
    if not 0 <= tolerance < 1:
        raise ValueError("tolerance must be in [0, 1)")
    fixed = tuple(phrases)

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
        for k, reason in enumerate(reasons):
            for c in _check_reason(reason, record, fixed, tolerance):
                checks.append({**c, "reason": k})
        return _result(checks)

    run.__doc__ = value_check_scorer.__doc__
    return register_task_scorer(run, name, version)
