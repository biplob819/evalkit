"""Attack cases and suites (SEC-01).

Lifted from assay-pipeline's `attack/suite.py` and `schemas/evaluation.py` (MIT, same author),
generalised: any tools, any layer vocabulary, held-out cases pinned by B5.14's manifest.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, ValidationError, model_validator

from evalkit import heldout as H
from evalkit.schemas import AttackBlocked, Trajectory
from evalkit.schemas._base import FrozenMap, JsonValue, Model, Name, NonBlank, PosInt
from evalkit.scorers import attack_outcome

from .errors import SecurityError
from .layers import SUCCESS_LAYERS, LayerSet

OWASP_CATEGORIES: tuple[str, ...] = tuple(f"LLM{i:02d}" for i in range(1, 11))
Owasp = Literal[
    "LLM01", "LLM02", "LLM03", "LLM04", "LLM05", "LLM06", "LLM07", "LLM08", "LLM09", "LLM10"
]
LayerName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
_SUFFIXES = (".jsonl", ".json", ".yaml", ".yml")


class ToolCall(Model):
    tool: Name
    arguments: FrozenMap[JsonValue] = Field(default_factory=dict)


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, list | tuple):
        for v in value:
            yield from _strings(v)
    elif value is not None:
        yield str(value)


def canary_in(texts: Iterable[str], canaries: Sequence[str]) -> bool:
    """Whether any canary occurs in any text, with `attack_outcome`'s folding (NFKC, format
    characters dropped, case-folded)."""
    if not canaries:
        return False
    gold = AttackBlocked(attack="canary-check", canaries=tuple(canaries))
    for text in texts:
        t = Trajectory(
            task_id="x",
            agent="x",
            trial=1,
            seed=0,
            steps=(),
            final_answer=text,
            sandbox_reset_ok=True,
            cost_usd=0.0,
        )
        if attack_outcome(gold, t, ()).details["leaks"]:
            return True
    return False


class AttackCase(Model):
    id: NonBlank
    owasp: Owasp
    technique: NonBlank | None = None
    prompt: NonBlank | None = None
    planted_data: FrozenMap[JsonValue] | None = None
    payloads: tuple[ToolCall, ...] = ()
    canaries: tuple[Name, ...] = ()
    expected_stop: Annotated[tuple[LayerName, ...], Field(min_length=1)]
    split: Literal["public", "heldout"]
    allowed_tools: tuple[Name, ...] | None = None
    k: PosInt | None = None  # this case's trials, overriding the run's

    @model_validator(mode="after")
    def _consistent(self) -> AttackCase:
        if self.prompt is None and self.planted_data is None and not self.payloads:
            raise ValueError("a case needs a prompt, planted data or payloads")
        if len(set(self.expected_stop)) != len(self.expected_stop):
            raise ValueError("expected_stop layers must be unique")
        if len(set(self.canaries)) != len(self.canaries):
            raise ValueError("canaries must be unique")
        own = [
            *([self.prompt] if self.prompt else []),
            *_strings(self.planted_data),
            *(s for p in self.payloads for s in _strings(p.arguments)),
        ]
        if canary_in(own, self.canaries):
            raise ValueError(
                "a canary appears in the case's own prompt, planted data or payloads "
                "(it would score a false leak)"
            )
        return self


def case_key(case: AttackCase) -> str:
    """How outputs name a case: its id when public, `heldout:<manifest id>` when held out."""
    return case.id if case.split == "public" else f"heldout:{H.manifest_id(case.id)}"


def case_hash(case: AttackCase) -> str:
    """First 16 hex characters of the sha256 of the case's canonical JSON: an edited case is a
    different case."""
    text = json.dumps(case.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def check_cases(cases: Sequence[AttackCase], layers: LayerSet | None = None) -> list[str]:
    """Problems with a set of cases under a layer set: repeated ids, and expected stops that
    are not blocking layers. Held-out cases are named by manifest id."""
    ls = layers if layers is not None else LayerSet()
    problems: list[str] = []
    counts = Counter(c.id for c in cases)
    named: set[str] = set()
    for c in cases:
        name = case_key(c)
        if counts[c.id] > 1 and name not in named:
            named.add(name)
            problems.append(f"{name}: the id repeats ({counts[c.id]} cases)")
        bad = [x for x in c.expected_stop if x not in ls.blocking]
        if bad:
            why = "a success layer" if set(bad) <= set(SUCCESS_LAYERS) else "not a blocking layer"
            problems.append(
                f"{name}: expected_stop {bad if c.split == 'public' else len(bad)} is {why} "
                f"(blocking layers: {list(ls.blocking)})"
            )
    return problems


def _files(path: Path) -> list[Path]:
    if path.is_dir():
        return sorted(p for p in path.iterdir() if p.is_file() and p.suffix in _SUFFIXES)
    return [path]


def _validate(row: Any, where: str, *, heldout: bool) -> AttackCase:
    """Validate one row. Errors name the field path; for a held-out row only the top-level
    field (keys inside it, and unknown field names, can be held-out text) and the error type.
    The error is raised outside the handler, so no exception in its chain carries the row."""
    parts: list[str] = []
    try:
        return AttackCase.model_validate(row)
    except ValidationError as exc:
        for err in exc.errors(include_input=False, include_url=False, include_context=False):
            loc = err["loc"]
            if not heldout:
                path = ".".join(str(x) for x in loc) or "case"
                parts.append(f"{path}: {err['type']} ({err['msg']})")
            elif not loc:
                parts.append(f"case: {err['type']}")
            elif err["type"] == "extra_forbidden" and len(loc) == 1:
                parts.append("an unknown field: extra_forbidden")
            elif str(loc[0]) in AttackCase.model_fields:
                parts.append(f"{loc[0]}: {err['type']}")
            else:
                parts.append(f"an unknown field: {err['type']}")
    parts = list(dict.fromkeys(parts))
    raise SecurityError(f"{where}: {'; '.join(parts)}")


def load_attack_cases(
    path: str | Path, *, split: str | None = None, layers: LayerSet | None = None
) -> tuple[AttackCase, ...]:
    """Attack cases from a YAML/JSON/JSONL file or a folder of them, validated. Errors name the
    file, the case position and the field, never a value."""
    p = Path(path)
    if not p.exists():
        raise SecurityError(f"{p}: no such case file or folder")
    out: list[AttackCase] = []
    for f in _files(p):
        problem: str | None = None
        rows: list[Any] = []
        try:
            rows = H.load_cases(f)
        except H.HeldoutError as exc:
            problem = str(exc)  # B5.14 names the file and position, never the text
        if problem is not None:
            raise SecurityError(problem)
        for i, row in enumerate(rows, start=1):
            # held out unless the file or the row itself says public (a bad row stays hidden)
            heldout = split == "heldout" or (
                split != "public" and not (isinstance(row, dict) and row.get("split") == "public")
            )
            case = _validate(row, f"{f.name}: case {i}", heldout=heldout)
            if split is not None and case.split != split:
                raise SecurityError(f"{f.name}: case {i}: split must be {split!r}")
            out.append(case)
    problems = check_cases(out, layers)
    if problems:
        raise SecurityError(f"{p.name}: " + "; ".join(problems))
    return tuple(out)


@dataclass(frozen=True)
class AttackSuite:
    cases: tuple[AttackCase, ...]
    public_path: Path | None = None
    heldout_path: Path | None = None
    manifest_path: Path | None = None

    @property
    def public(self) -> tuple[AttackCase, ...]:
        return tuple(c for c in self.cases if c.split == "public")

    @property
    def heldout(self) -> tuple[AttackCase, ...]:
        return tuple(c for c in self.cases if c.split == "heldout")

    def fingerprint(self) -> str:
        """sha256 of the sorted (case key, case hash) pairs and, with held-out cases, the
        manifest's own hash: a resume or report against another suite is detectable."""
        pairs = sorted((case_key(c), case_hash(c)) for c in self.cases)
        manifest = None
        if self.heldout and self.manifest_path is not None:
            try:
                data = json.loads(Path(self.manifest_path).read_text(encoding="utf-8"))
                manifest = data.get("manifest_sha256") if isinstance(data, dict) else None
            except (OSError, ValueError):
                manifest = None
        body = json.dumps({"cases": pairs, "manifest": manifest}, sort_keys=True)
        return hashlib.sha256(body.encode("utf-8")).hexdigest()

    def verify(self) -> None:
        """Refuse held-out cases whose file no longer matches its manifest, or that have no
        manifest at all."""
        if not self.heldout and self.heldout_path is None:
            return
        if self.heldout_path is None or self.manifest_path is None:
            raise SecurityError("held-out cases need their file and a B5.14 manifest to verify")
        problems = _manifest_problems(self.manifest_path, self.heldout_path, self.public_path)
        if problems:
            raise SecurityError("held-out cases do not match the manifest: " + "; ".join(problems))
        # the cases in memory must be exactly the ones the manifest pins in the file
        pinned = sorted(case_hash(c) for c in load_attack_cases(self.heldout_path, split="heldout"))
        held = sorted(case_hash(c) for c in self.heldout)
        if pinned != held:
            differ = len(set(pinned) ^ set(held))
            raise SecurityError(
                f"the held-out cases in memory do not match the manifest-pinned file "
                f"({differ} case(s) differ)"
            )


def _manifest_problems(manifest: Path, heldout: Path, public: Path | None) -> list[str]:
    splits: dict[str, str | Path] = {"heldout": heldout}
    try:
        recorded = json.loads(Path(manifest).read_text(encoding="utf-8"))
        pinned = set(recorded.get("counts", {})) if isinstance(recorded, dict) else set()
    except (OSError, ValueError):
        pinned = set()
    if "public" in pinned and public is not None:
        splits["public"] = public
    return H.verify_manifest(manifest, splits)


def load_suite(
    public: str | Path,
    *,
    heldout: str | Path | None = None,
    manifest: str | Path | None = None,
    layers: LayerSet | None = None,
) -> AttackSuite:
    """The public cases and, when given, the held-out cases, whose file must match the manifest
    before it is read."""
    pub_path = Path(public)
    cases = load_attack_cases(pub_path, split="public", layers=layers)
    held_path = man_path = None
    if heldout is not None:
        if manifest is None:
            raise SecurityError("held-out cases need a B5.14 manifest (--manifest)")
        held_path, man_path = Path(heldout), Path(manifest)
        problems = _manifest_problems(man_path, held_path, pub_path)
        if problems:
            raise SecurityError("held-out cases do not match the manifest: " + "; ".join(problems))
        cases += load_attack_cases(held_path, split="heldout", layers=layers)
    elif manifest is not None:
        raise SecurityError("a manifest was given without held-out cases (--heldout)")
    problems = check_cases(cases, layers)
    if problems:
        raise SecurityError("attack suite: " + "; ".join(problems))
    return AttackSuite(cases, pub_path, held_path, man_path)
