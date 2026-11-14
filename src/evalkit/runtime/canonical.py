"""Canonical JSON: the single byte representation used for cache keys and hashes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel


def to_jsonable(obj: Any) -> Any:
    """Convert models, mappings and sequences into plain JSON types."""
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            sk = str(k)
            if sk in out:
                raise ValueError(f"keys {k!r} and {sk!r} collide in canonical JSON")
            out[sk] = to_jsonable(v)
        return out
    if isinstance(obj, list | tuple):
        return [to_jsonable(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> str:
    """Sorted keys, no whitespace, UTF-8, floats via repr, NaN and infinity refused."""
    return json.dumps(
        to_jsonable(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_hex(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()
