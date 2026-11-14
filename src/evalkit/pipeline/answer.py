"""The structured answer: its JSON schema and a strict parser (B1, GEN-02)."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from evalkit.schemas import Answer


class SchemaError(ValueError):
    """The model's output is not a valid answer. Scored as error="schema", never retried."""


def answer_schema(labels: tuple[str, ...] | list[str]) -> dict[str, Any]:
    """Strict JSON schema sent as structured output. Range checks on citations happen in the
    citation scorer, so only portable keywords are used here."""
    return {
        "type": "object",
        "properties": {
            "label": {"type": "string", "enum": list(labels)},
            "answer": {"type": "string"},
            "citations": {"type": "array", "items": {"type": "integer"}},
        },
        "required": ["label", "answer", "citations"],
        "additionalProperties": False,
    }


_FENCED = re.compile(r"^```(?:json)?[ \t]*\n(?P<body>.*)\n```$", re.IGNORECASE | re.DOTALL)


def _extract_json(text: str) -> str:
    """The whole output must be one JSON object, optionally inside a single ```json fence.
    Prose around an object is a schema error: a JSON example inside an explanation must
    never be scored as the answer."""
    text = text.strip()
    m = _FENCED.match(text)
    if m:
        text = m.group("body").strip()
    if not (text.startswith("{") and text.endswith("}")):
        raise SchemaError("output is not a single JSON object")
    return text


def parse_answer(raw_text: str, labels: tuple[str, ...] | list[str]) -> Answer:
    """Parse and validate one answer. The label is matched case-insensitively and returned
    in the task's own spelling. Anything else wrong is a SchemaError."""
    try:
        data = json.loads(_extract_json(raw_text))
    except ValueError as exc:
        raise SchemaError(f"output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SchemaError("output JSON is not an object")
    canonical = {lab.casefold(): lab for lab in labels}
    label = data.get("label")
    if not isinstance(label, str) or label.strip().casefold() not in canonical:
        raise SchemaError(f"label {label!r} is not one of {list(labels)}")
    data["label"] = canonical[label.strip().casefold()]
    try:
        return Answer.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        loc = ".".join(str(x) for x in first["loc"])
        raise SchemaError(f"invalid answer at {loc or 'top level'}: {first['msg']}") from exc
