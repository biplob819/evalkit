"""Rule compliance (RUL-01): a decision's cited rules exist, a cited allow rule holds, and no
never rule holds on an allow.

Predicate parsing and evaluation are ported from fraud-review-bedrock
`src/fraudreview/policy.py` (`parse_predicate`, `Clause.holds`, `Policy.clearing_allowed`) and
`router.check_clear` (MIT licence, Copyright (c) 2026 Biplob Chakraborty, the same author),
generalised from its alert fields to any named, typed fields.

Predicates are parsed with `ast` against a node allow-list, type-checked against the rule set's
fields when it is built, and evaluated by walking the tree; nothing is ever executed. Missing
values (absent, None, non-finite) fail safe: an allow rule does not hold, a never rule holds and
a signal does not hold.
"""

from __future__ import annotations

import ast
import json
import math
import re
from collections.abc import Mapping
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from evalkit.schemas import Case, Completion
from evalkit.schemas._base import FrozenMap, Model, Name, NonBlank
from evalkit.scorers.base import (
    FunctionScorer,
    Scorer,
    ScoreResult,
    _register,
    normalise_label,
)

MAX_PREDICATE = 300

RuleKind = Literal["allow", "never", "signal"]
FieldType = Literal["number", "bool", "text"] | Annotated[tuple[NonBlank, ...], Field(min_length=1)]

_ALLOWED_NODES = (
    ast.Expression,
    ast.BoolOp,
    ast.And,
    ast.Or,
    ast.UnaryOp,
    ast.Not,
    ast.USub,
    ast.Compare,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
    ast.Eq,
    ast.NotEq,
    ast.Name,
    ast.Load,
    ast.Constant,
)
_ORDERING = (ast.Lt, ast.LtE, ast.Gt, ast.GtE)
_OPS = {
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}
_PLAIN_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,6})?")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class RuleError(ValueError):
    """A rule set, predicate or record is malformed; the message names the rule and problem."""


# ------------------------------------------------------------------ predicate parsing


def _kind_of(spec: Any) -> str:
    """'number', 'bool', 'text' or 'category' for a field's declared type."""
    return spec if isinstance(spec, str) else "category"


def _operand_type(node: ast.expr, fields: Mapping[str, Any] | None, rid: str, src: str) -> str:
    """The type of one side of a comparison ('?' for a field of unknown type)."""
    if isinstance(node, ast.Name):
        if fields is None:
            return "?"
        return _kind_of(fields[node.id])
    negative = isinstance(node, ast.UnaryOp)
    if negative:
        assert isinstance(node, ast.UnaryOp)
        if not isinstance(node.op, ast.USub):
            raise RuleError(f"{rid}: only a number can be negative")
        node = node.operand
    if not isinstance(node, ast.Constant):
        raise RuleError(f"{rid}: compare a field with a field or a constant, not an expression")
    value = node.value
    if isinstance(value, bool):
        if negative:
            raise RuleError(f"{rid}: only a number can be negative")
        return "bool"
    if isinstance(value, int | float):
        written = ast.get_source_segment(src, node) or ""
        if not math.isfinite(value) or not _PLAIN_DECIMAL.fullmatch(written):
            raise RuleError(f"{rid}: write {written!r} as a plain decimal (at most 6 places)")
        return "number"
    if isinstance(value, str) and not negative:
        if ast.get_source_segment(src, node) not in (f"'{value}'", f'"{value}"'):
            raise RuleError(f"{rid}: write each string as one plain quoted string")
        return "string"
    raise RuleError(f"{rid}: constant {value!r} is not allowed")


def _unify(types: list[str], rid: str) -> str:
    """One type for a comparison's operands; a string constant fits a text or category field."""
    known = {t for t in types if t != "?"}
    if "string" in known and known - {"string"} <= {"text", "category"}:
        known.discard("string")
        if not known:
            known = {"text"}
    if len(known) > 1:
        raise RuleError(f"{rid}: compares values of types {sorted(known)}")
    return known.pop() if known else "?"


def _check_boolean(node: ast.expr, fields: Mapping[str, Any] | None, rid: str, src: str) -> None:
    """Every condition is a true/false test: a comparison that reads a field, a bool field, or
    and/or/not of those. A bare number or constant is refused: its truth would be accidental."""
    if isinstance(node, ast.BoolOp):
        for value in node.values:
            _check_boolean(value, fields, rid, src)
    elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        _check_boolean(node.operand, fields, rid, src)
    elif isinstance(node, ast.Name):
        if fields is not None and fields[node.id] != "bool":
            raise RuleError(f"{rid}: {node.id} is not true/false; compare it with a value")
    elif isinstance(node, ast.Compare):
        operands = [node.left, *node.comparators]
        if not any(isinstance(o, ast.Name) for o in operands):
            raise RuleError(f"{rid}: a comparison must read a field")
        types = [_operand_type(o, fields, rid, src) for o in operands]
        kind = _unify(types, rid)
        ordering = any(isinstance(op, _ORDERING) for op in node.ops)
        if ordering and (kind not in ("number", "?") or "string" in types or "bool" in types):
            raise RuleError(f"{rid}: only numbers can be ordered; use == or != otherwise")
        if fields is not None and kind == "category":
            allowed = next(fields[o.id] for o in operands if isinstance(o, ast.Name))
            for o in operands:
                if isinstance(o, ast.Constant) and o.value not in allowed:
                    raise RuleError(f"{rid}: {o.value!r} is not one of the field's values")
    else:
        raise RuleError(f"{rid}: a predicate must be a true/false test")


def parse_predicate(when: str, fields: Mapping[str, Any] | None, rid: str) -> ast.Expression:
    """Parse and check one predicate. With `fields` (name -> FieldType) every name must be a
    declared field and every comparison is type-checked; with None (an allow predicate over a
    decision) types are checked when it is evaluated."""
    if not isinstance(when, str) or not when.strip():
        raise RuleError(f"{rid}: a predicate cannot be empty")
    if not when.isascii():
        raise RuleError(f"{rid}: a predicate is plain ASCII")
    if "#" in when:
        raise RuleError(f"{rid}: comments are not allowed in a predicate")
    if re.search(r"\s\s|[^\S ]", when):
        raise RuleError(f"{rid}: a predicate is one line with single spaces")
    if len(when) > MAX_PREDICATE:
        raise RuleError(f"{rid}: a predicate is at most {MAX_PREDICATE} characters")
    src = when.strip()
    try:
        tree = ast.parse(src, mode="eval")
    except SyntaxError as exc:
        raise RuleError(f"{rid}: predicate does not parse: {exc.msg}") from exc
    except (RecursionError, MemoryError, ValueError) as exc:
        raise RuleError(f"{rid}: predicate is nested too deeply") from exc
    for node in ast.walk(tree):
        if not isinstance(node, _ALLOWED_NODES):
            raise RuleError(f"{rid}: {type(node).__name__} is not allowed in a predicate")
        if isinstance(node, ast.Name) and not _NAME.fullmatch(node.id):
            raise RuleError(f"{rid}: {node.id!r} is not a plain field name")
    if fields is not None:
        unknown = sorted({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} - set(fields))
        if unknown:
            raise RuleError(f"{rid}: reads unknown field(s) {unknown}")
    try:
        _check_boolean(tree.body, fields, rid, src)
    except RecursionError as exc:
        raise RuleError(f"{rid}: predicate is nested too deeply") from exc
    return tree


def _names(tree: ast.AST) -> tuple[str, ...]:
    return tuple(dict.fromkeys(n.id for n in ast.walk(tree) if isinstance(n, ast.Name)))


def _missing(value: Any) -> bool:
    return value is None or (isinstance(value, float) and not math.isfinite(value))


class _Unreadable(Exception):
    """A value's type does not fit the comparison (only for untyped allow predicates)."""


class _Refused(Exception):
    """An untyped allow predicate orders values that are not numbers, or comparing them
    raised TypeError."""


def _evaluate(node: ast.AST, values: Mapping[str, Any], strict: bool) -> Any:
    if isinstance(node, ast.BoolOp):
        results = (_truth(_evaluate(v, values, strict), strict) for v in node.values)
        return all(results) if isinstance(node.op, ast.And) else any(results)
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.Not):
            return not _truth(_evaluate(node.operand, values, strict), strict)
        operand = node.operand
        # the parser allows `-` on number constants only
        assert isinstance(operand, ast.Constant) and isinstance(operand.value, int | float)
        return -operand.value
    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, values, strict)
        for op, comparator in zip(node.ops, node.comparators, strict=True):
            right = _evaluate(comparator, values, strict)
            a, b = left, right
            if not strict:
                kind = _type(left)
                if isinstance(op, _ORDERING) and (kind != "number" or _type(right) != "number"):
                    raise _Refused
                if kind != _type(right):
                    raise _Unreadable
                if kind == "text":  # "Clear" == "clear": NFKC, trimmed, case-folded
                    a, b = normalise_label(left), normalise_label(right)
            try:
                if not _OPS[type(op)](a, b):
                    return False
            except TypeError as exc:
                if strict:  # pragma: no cover - the type check prevents this
                    raise
                raise _Refused from exc
            left = right
        return True
    if isinstance(node, ast.Name):
        return values[node.id]
    if isinstance(node, ast.Constant):
        return node.value
    raise RuleError(f"unsupported predicate node {type(node).__name__}")  # parser prevents this


def _truth(value: Any, strict: bool) -> bool:
    if not isinstance(value, bool):
        if strict:  # pragma: no cover - the type check refuses non-bool conditions
            raise RuleError("a condition is not true/false")
        raise _Unreadable
    return value


def _type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int | float):
        return "number"
    if isinstance(value, str):
        return "text"
    return "other"


# ------------------------------------------------------------------ rule sets


class Rule(Model):
    """One rule: an id, a kind and a predicate over the rule set's fields."""

    id: Name
    kind: RuleKind
    when: NonBlank
    title: str = ""


class RuleSet(Model):
    """Rules over named, typed fields. Building one parses and type-checks every predicate."""

    rules: Annotated[tuple[Rule, ...], Field(min_length=1)]
    fields: FrozenMap[FieldType]

    @model_validator(mode="after")
    def _check(self) -> RuleSet:
        ids = [r.id for r in self.rules]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise RuleError(f"duplicate rule id(s) {dupes}")
        for name in self.fields:
            if not _NAME.fullmatch(name):
                raise RuleError(f"field {name!r} is not a plain name")
        for r in self.rules:
            parse_predicate(r.when, self.fields, r.id)
        if not any(r.kind == "allow" for r in self.rules):
            raise RuleError("the rule set has no allow rule, so nothing could be allowed")
        return self

    def rule(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.id == rule_id:
                return r
        raise KeyError(f"no rule {rule_id!r}")

    def missing(self, rule_id: str, record: Mapping[str, Any]) -> tuple[str, ...]:
        """The fields this rule reads that have no usable value in `record`."""
        tree = ast.parse(self.rule(rule_id).when.strip(), mode="eval")
        return tuple(f for f in _names(tree) if _missing(record.get(f)))

    def holds(self, rule_id: str, record: Mapping[str, Any]) -> bool:
        """Evaluate one rule on a record; missing values fail safe by kind."""
        r = self.rule(rule_id)
        if self.missing(rule_id, record):
            return r.kind == "never"
        tree = ast.parse(r.when.strip(), mode="eval")  # checked when the set was built
        self.check_record(record, _names(tree))
        return bool(_evaluate(tree.body, record, strict=True))

    def check_record(self, record: Mapping[str, Any], names: Any = None) -> None:
        """RuleError when a present, non-missing field has a type other than the declared one
        (all declared fields, or only `names`)."""
        for name in self.fields if names is None else names:
            if name not in record or _missing(record[name]):
                continue
            spec = self.fields[name]
            want, got = _kind_of(spec), _type(record[name])
            if got != {"category": "text"}.get(want, want):
                raise RuleError(
                    f"record field {name!r} is {type(record[name]).__name__}, the rule set "
                    f"declares {want}"
                )
            if want == "category" and record[name] not in spec:
                raise RuleError(
                    f"record field {name!r} is {record[name]!r}, not one of its declared "
                    f"category values"
                )


def _is_allow(decision: Mapping[str, Any], tree: ast.Expression | None) -> tuple[bool, bool]:
    """(whether the decision is an allow, whether the predicate hit a type error). Unreadable
    (a missing field or a type that does not fit) counts as an allow, so the rules are
    checked: fail safe. A type error (ordering values that are not numbers, or a comparison
    Python refuses) also counts as an allow and fails the decision."""
    if tree is None:
        return True, False
    if any(_missing(decision.get(n)) for n in _names(tree)):
        return True, False
    try:
        return _truth(_evaluate(tree.body, decision, strict=False), strict=False), False
    except _Unreadable:
        return True, False
    except (_Refused, TypeError):
        return True, True


def rule_compliance(
    decision: Mapping[str, Any],
    record: Mapping[str, Any],
    rules: RuleSet,
    *,
    cited_key: str = "rule_ids",
    allow: str | None = None,
) -> ScoreResult:
    """Check one decision against a rule set on the record it was made on.

    `allow` is a predicate over the decision; its text values compare NFKC-casefolded and
    trimmed (`"Clear" == "clear"`), and ordering (`< <= > >=`) reads numbers only.

    Reasons, in order: `unknown_rule:<id>` for each cited id not in the set and `bad_citations`
    when `decision[cited_key]` is not a list of strings (any decision); `type_error:allow` when
    the allow predicate orders values that are not numbers or cannot compare them (the decision
    then counts as an allow); then, on an allow only: `no_allow_rule_cited`, or, when no cited
    allow rule holds, `predicate_false:<id>` or `missing_value:<id>` per cited allow rule; then
    `never:<id>` for every never rule in the set that holds, cited or not. 1.0 with no reasons,
    else 0.0."""
    tree = parse_predicate(allow, None, "allow") if allow is not None else None
    return _compliance((decision,), record, rules, cited_key, tree)


def _compliance(
    views: tuple[Mapping[str, Any], ...],
    record: Mapping[str, Any],
    rules: RuleSet,
    cited_key: str,
    tree: ast.Expression | None,
) -> ScoreResult:
    """`rule_compliance` over one or more readings of a decision: cited ids come from the
    first; the decision is an allow when any reading is one (fail closed)."""
    rules.check_record(record)
    decision = views[0]
    raw = decision.get(cited_key, ())
    reasons: list[str] = []
    if isinstance(raw, list | tuple) and all(isinstance(i, str) for i in raw):
        cited = list(dict.fromkeys(raw))
    else:
        cited = []
    known = {r.id for r in rules.rules}
    reasons += [f"unknown_rule:{i}" for i in cited if i not in known]
    if not (isinstance(raw, list | tuple) and all(isinstance(i, str) for i in raw)):
        reasons.append("bad_citations")
    readings = [_is_allow(v, tree) for v in views]
    is_allow = any(a for a, _ in readings)
    if any(e for _, e in readings):
        reasons.append("type_error:allow")
    report: dict[str, dict[str, Any]] = {}

    def look(rule_id: str) -> bool:
        if rule_id not in report:
            r = rules.rule(rule_id)
            report[rule_id] = {
                "kind": r.kind,
                "holds": rules.holds(rule_id, record),
                "missing": list(rules.missing(rule_id, record)),
            }
        return report[rule_id]["holds"]

    for i in cited:
        if i in known:
            look(i)
    if is_allow:
        allows = [i for i in cited if i in known and rules.rule(i).kind == "allow"]
        if not allows:
            reasons.append("no_allow_rule_cited")
        elif not any(look(i) for i in allows):
            reasons += [
                f"{'missing_value' if report[i]['missing'] else 'predicate_false'}:{i}"
                for i in allows
            ]
        reasons += [f"never:{r.id}" for r in rules.rules if r.kind == "never" and look(r.id)]
    return ScoreResult(
        value=0.0 if reasons else 1.0,
        details={"allow": is_allow, "cited": cited, "reasons": reasons, "rules": report},
    )


def _answer_json(completion: Completion) -> dict[str, Any]:
    """The JSON object written as the answer text, else {}."""
    assert completion.answer is not None
    text = completion.answer.answer.strip()
    if not text.startswith("{"):
        return {}
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return {}
    return data if isinstance(data, dict) else {}


def _structured(completion: Completion) -> dict[str, Any]:
    assert completion.answer is not None
    ans = completion.answer
    return {"label": ans.label, "answer": ans.answer, "citations": list(ans.citations)}


def decision_fields(completion: Completion) -> dict[str, Any]:
    """A completion as a decision: its answer's `label`, `answer` and `citations`, plus every
    other key of a JSON object written as the answer text. The structured fields win: a JSON
    `label`, `answer` or `citations` key does not replace them."""
    return {**_answer_json(completion), **_structured(completion)}


def _decision_views(completion: Completion) -> tuple[dict[str, Any], ...]:
    """`decision_fields`, then (when the answer JSON repeats a structured key with another
    value) the same with the JSON keys winning, so either reading can make it an allow."""
    data = _answer_json(completion)
    first = {**data, **_structured(completion)}
    second = {**first, **data}
    return (first,) if second == first else (first, second)


def register_task_scorer(fn: Any, name: str, version: str) -> Scorer:
    """Register a factory-made scorer under `name`, never as a built-in (a project ships it
    as its own plugin)."""
    s = FunctionScorer(fn, name, version)
    _register(s, builtin=False)
    return s


def rule_compliance_scorer(
    rules: RuleSet,
    *,
    name: str = "rule_compliance",
    cited_key: str = "rule_ids",
    allow: str | None = None,
    record_key: str = "record",
    version: str = "1",
) -> Scorer:
    """Register a task scorer that runs `rule_compliance` on each record: the record from
    `case.meta[record_key]`, the decision from the completion (`decision_fields`: the
    structured label, answer and citations win over the same keys in an answer JSON, and the
    decision is an allow when either reading is one). A failed call scores 0.0."""
    # refuse a bad predicate before registering
    tree = parse_predicate(allow, None, "allow") if allow is not None else None

    def run(case: Case, completion: Completion, **_: Any) -> ScoreResult:
        if completion.error is not None or completion.answer is None:
            return ScoreResult(value=0.0, details={"reason": completion.error or "no_answer"})
        record = case.meta.get(record_key)
        if not isinstance(record, Mapping):
            raise RuleError(f"case {case.id!r} has no {record_key!r} object in its meta")
        return _compliance(_decision_views(completion), record, rules, cited_key, tree)

    run.__doc__ = rule_compliance_scorer.__doc__
    return register_task_scorer(run, name, version)
