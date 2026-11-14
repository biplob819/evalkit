"""Content-addressed store (RUN-02) with lineage sidecars (RUN-03)."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict, Field

M = TypeVar("M", bound=BaseModel)


class Lineage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    node: str
    parents: tuple[str, ...] = ()
    started_at: datetime
    finished_at: datetime
    cost_usd: float = Field(ge=0, allow_inf_nan=False)


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename: never a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


class Store:
    """Outputs at `<root>/<key[:2]>/<key>.json`, lineage at `<key>.lineage.json`."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path(self, key: str, suffix: str = ".json") -> Path:
        if len(key) != 64 or not all(c in "0123456789abcdef" for c in key):
            raise ValueError(f"not a cache key (64 lowercase hex characters): {key!r}")
        return self.root / key[:2] / f"{key}{suffix}"

    def has(self, key: str) -> bool:
        return self.path(key).is_file()

    def get(self, key: str, model: type[M]) -> M | None:
        p = self.path(key)
        if not p.is_file():
            return None
        return model.model_validate_json(p.read_text(encoding="utf-8"))

    def put(self, key: str, value: BaseModel, lineage: Lineage) -> None:
        if lineage.key != key:
            raise ValueError("lineage key does not match the stored key")
        # Lineage first: an output never exists without its lineage.
        _atomic_write(self.path(key, ".lineage.json"), lineage.model_dump_json())
        _atomic_write(self.path(key), value.model_dump_json())

    def lineage(self, key: str) -> Lineage | None:
        p = self.path(key, ".lineage.json")
        return Lineage.model_validate_json(p.read_text(encoding="utf-8")) if p.is_file() else None

    # Small named side records, e.g. a submitted batch id that a resumed run must reuse.
    def put_meta(self, key: str, name: str, data: dict[str, Any]) -> None:
        _atomic_write(self.path(key, f".{name}.json"), json.dumps(data, sort_keys=True))

    def get_meta(self, key: str, name: str) -> dict[str, Any] | None:
        p = self.path(key, f".{name}.json")
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
