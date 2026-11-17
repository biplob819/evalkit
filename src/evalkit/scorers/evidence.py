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
    """One `{...}`: `name`, `name:spec` or `name?if true|if false`."""
    name, sep, rest = body, "", ""
    for mark in (":", "?"):
        if mark in body:
            name, sep, rest = body.partition(mark)
            break
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


_COMPARATOR = re.compile(
    r"(no more than|no less than|more than|greater than|fewer than|less than|at least|"
    r"at most|exceeding|exceeds|above|below|under|over|>=|<=|≥|≤|>|<)\s*[$£€]?\s*$",
    re.IGNORECASE,
)
_OP = {
    "no more than": "<=",
    "at most": "<=",
    "<=": "<=",
    "≤": "<=",
    "no less than": ">=",
    "at least": ">=",
    ">=": ">=",
    "≥": ">=",
    "more than": ">",
    "greater than": ">",
    "exceeding": ">",
    "exceeds": ">",
    "above": ">",
    "over": ">",
    ">": ">",
    "fewer than": "<",
    "less than": "<",
    "below": "<",
    "under": "<",
    "<": "<",
}


def _close(n: Number, v: float, tolerance: float) -> bool:
    """`n` equals |v| to its displayed precision or within the relative tolerance; a percent
    may also equal v as a fraction."""
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
    x, v = n.value, abs(v)
    return {">": v > x, ">=": v >= x, "<": v < x, "<=": v <= x}[op]


def _numeric(record: Mapping[str, Any], name: str, where: str) -> float | None:
    if name not in record:
        raise RuleError(f"{where}: the record has no field {name!r}")
    value = record[name]
    if _missing(value):
        return None
    if _type(value) != "number":
        raise RuleError(f"{where}: record field {name!r} is not a number")
    return float(value)


def _condition(holds: str, record: Mapping[str, Any], where: str) -> tuple[bool, dict[str, Any]]:
    tree = parse_predicate(holds, None, where)
    names = _names(tree)
    for name in names:
        if name not in record:
            raise RuleError(f"{where}: the record has no field {name!r}")
    found = {n: record[n] for n in names}
    if any(_missing(v) for v in found.values()):
        return False, {n: (None if _missing(v) else v) for n, v in found.items()}
    types: dict[str, Any] = {}
    for name, value in found.items():
        kind = _type(value)
        if kind == "other":
            raise RuleError(f"{where}: record field {name!r} is not a number, bool or text")
        types[name] = kind
    typed = parse_predicate(holds, types, where)
    return bool(_evaluate(typed.body, record, strict=True)), found


def _finite(n: Number) -> bool:
    return all(math.isfinite(x) and math.isfinite(h) for x, h, _ in n.candidates)


def _check_reason(
    reason: str, record: Mapping[str, Any], phrases: Sequence[ValuePhrase], tolerance: float
) -> list[dict[str, Any]]:
    numbers = extract_numbers(reason)
    used: set[int] = set()
    checks: list[dict[str, Any]] = []
    for ph in phrases:
        where = f"phrase {ph.pattern!r}"
        for m in re.finditer(ph.pattern, reason, re.IGNORECASE):
            if ph.holds is not None:
                ok, found = _condition(ph.holds, record, where)
                checks.append(
                    {
                        "text": m.group(0),
                        "kind": "condition",
                        "holds": ph.holds,
                        "found": found,
                        "ok": ok,
                    }
                )
                continue
            assert ph.field is not None
            inside = [
                i for i, n in enumerate(numbers) if m.start() <= n.start < m.end() and i not in used
            ]
            if not inside:
                continue  # the phrase states no number here
            i = inside[0]
            used.add(i)
            n = numbers[i]
            cmp = _COMPARATOR.search(reason[: n.start])
            op = _OP[cmp.group(1).lower()] if cmp else "≈"
            value = _numeric(record, ph.field, where)
            ok = value is not None and _finite(n) and _claim(n, op, value, tolerance)
            checks.append(
                {
                    "text": m.group(0),
                    "kind": "value",
                    "field": ph.field,
                    "op": op,
                    "claimed": n.value if _finite(n) else None,
                    "found": value,
                    "ok": ok,
                }
            )
    numeric = {k: float(v) for k, v in record.items() if not _missing(v) and _type(v) == "number"}
    for i, n in enumerate(numbers):
        if i in used:
            continue
        match = next((k for k, v in numeric.items() if _close(n, v, tolerance)), None)
        ok = _finite(n) and match is not None
        checks.append(
            {
                "text": n.text,
                "kind": "number",
                "field": match,
                "claimed": n.value if _finite(n) else None,
                "found": numeric[match] if match else None,
                "ok": ok,
            }
        )
    return checks


def value_check(
    reason: str,
    record: Mapping[str, Any],
    *,
    phrases: Sequence[ValuePhrase] = (),
    tolerance: float = 0.1,
) -> ScoreResult:
    """Check every number and declared phrase in `reason` against `record`. 1.0 when nothing
    fails (also when nothing was checkable), else 0.0. Details: `checks`, `failed` (the text of
    each failed check), `n_checked`. Signs are ignored, as in AFR-01; a missing record value
    fails the claim that needs it."""
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
