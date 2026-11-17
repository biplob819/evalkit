"""The model-free scripted attacker (SEC-04).

Lifted from assay-pipeline's `attack/oracle.py` (MIT, same author), generalised: each case's
payloads go straight to the system's tools (`ToolSurface`) with no model in the loop, and every
payload must be stopped by a layer the case expects.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import ValidationError, model_validator

from evalkit.schemas._base import Model, NonBlank

from .cases import AttackCase, AttackSuite, canary_in, case_key, check_cases
from .errors import SecurityError
from .layers import LayerSet
from .trials import call_guarded

SCRIPTED_FILE = "scripted.jsonl"


@dataclass(frozen=True)
class ToolReply:
    text: str = ""
    error: str | None = None
    layer: str | None = None  # the layer the system says stopped the call


class ToolSurface(Protocol):
    def call(self, tool: str, arguments: Mapping[str, Any]) -> ToolReply: ...


class ScriptedResult(Model):
    case: NonBlank
    split: Literal["public", "heldout"]
    owasp: NonBlank | None  # None for a held-out case
    layers: tuple[str, ...]
    passed: bool
    detail: str = ""
    skipped: bool = False  # the case has no payloads: nothing to send without a model

    @model_validator(mode="after")
    def _consistent(self) -> ScriptedResult:
        if (self.owasp is None) != (self.split == "heldout"):
            raise ValueError("a public result names its category; a held-out one does not")
        if self.split == "heldout" and self.detail:
            raise ValueError("a held-out result carries no detail")
        if self.skipped and (self.passed or self.layers):
            raise ValueError("a skipped case has no layers and does not pass")
        return self


def _owasp(case: AttackCase) -> str | None:
    return case.owasp if case.split == "public" else None


def _payload_layer(case: AttackCase, reply: ToolReply, ls: LayerSet) -> str:
    texts = [reply.text, *([reply.error] if reply.error else [])]
    if canary_in(texts, case.canaries):
        return "leak"
    if reply.layer is not None:
        return ls.check_blocking(reply.layer, "the reported layer")
    return ls.classify(reply.error) or "executed"


def scripted_attack(
    case: AttackCase, surface: ToolSurface, *, layers: LayerSet | None = None
) -> ScriptedResult:
    """Send each payload to the tools; the case passes only if every payload is stopped by a
    layer in its expected_stop."""
    ls = layers if layers is not None else LayerSet()
    if not case.payloads:
        raise SecurityError(f"{case_key(case)}: no payloads for the scripted attacker")
    found: list[str] = []
    for call in case.payloads:
        reply = call_guarded(
            case, "the tool surface", lambda c=call: surface.call(c.tool, dict(c.arguments))
        )
        if not isinstance(reply, ToolReply):
            raise SecurityError(
                f"the tool surface returned {type(reply).__name__} on {case_key(case)}, "
                "not a ToolReply"
            )
        found.append(_payload_layer(case, reply, ls))
    passed = all(x in case.expected_stop for x in found)
    detail = ""
    if case.split == "public":
        detail = "; ".join(
            f"payload {i} ({p.tool}): {x}"
            for i, (p, x) in enumerate(zip(case.payloads, found, strict=True), start=1)
        )
    return ScriptedResult(
        case=case_key(case),
        split=case.split,
        owasp=_owasp(case),
        layers=tuple(found),
        passed=passed,
        detail=detail,
    )


def run_scripted(
    suite: AttackSuite,
    surface: ToolSurface,
    *,
    layers: LayerSet | None = None,
    run_dir: str | Path | None = None,
) -> list[ScriptedResult]:
    """Every case with payloads through the scripted attacker, after the manifest check; cases
    without payloads are recorded as skipped."""
    ls = layers if layers is not None else LayerSet()
    suite.verify()
    problems = check_cases(suite.cases, ls)
    if problems:
        raise SecurityError("attack suite: " + "; ".join(problems))
    results = [
        scripted_attack(c, surface, layers=ls)
        if c.payloads
        else ScriptedResult(
            case=case_key(c), split=c.split, owasp=_owasp(c), layers=(), passed=False, skipped=True
        )
        for c in suite.cases
    ]
    if run_dir is not None:
        d = Path(run_dir)
        d.mkdir(parents=True, exist_ok=True)
        text = "".join(
            json.dumps(r.model_dump(mode="json"), sort_keys=True) + "\n" for r in results
        )
        (d / SCRIPTED_FILE).write_text(text, encoding="utf-8", newline="\n")
    return results


def read_scripted(path: str | Path) -> list[ScriptedResult]:
    p = Path(path)
    out: list[ScriptedResult] = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SecurityError(f"{p.name}: cannot read ({type(exc).__name__})") from None
    for i, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            out.append(ScriptedResult.model_validate_json(line))
        except ValidationError:
            raise SecurityError(f"{p.name}: line {i} is not a scripted result") from None
    return out


def scripted_counts(results: Sequence[ScriptedResult]) -> dict[str, int]:
    ran = [r for r in results if not r.skipped]
    return {
        "cases": len(ran),
        "passed": sum(r.passed for r in ran),
        "skipped": len(results) - len(ran),
    }
