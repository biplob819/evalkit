"""Content-addressed store (RUN-02) with lineage sidecars (RUN-03)."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
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
        _replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


def _replace(tmp: str, path: Path, attempts: int = 8) -> None:
    """os.replace, tolerating Windows' refusal to replace a file another writer is replacing.

    The store is content-addressed and first-writer-wins: if the rename keeps being refused
    and the target exists, another writer has already stored this key, so ours is dropped.
    """
    for i in range(attempts):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if i == attempts - 1:
                if path.exists():
                    os.unlink(tmp)
                    return
                raise
            time.sleep(0.005 * (i + 1))


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

    def put(self, key: str, value: BaseModel, lineage: Lineage, *, overwrite: bool = False) -> bool:
        """Store an output. First writer wins: returns False without writing if the key is
        already stored, unless `overwrite` (used to replace a corrupt or stale entry)."""
        if lineage.key != key:
            raise ValueError("lineage key does not match the stored key")
        if not overwrite and self.has(key):
            return False
        # Lineage first: an output never exists without its lineage.
        _atomic_write(self.path(key, ".lineage.json"), lineage.model_dump_json())
        _atomic_write(self.path(key), value.model_dump_json())
        return True

    def lineage(self, key: str) -> Lineage | None:
        p = self.path(key, ".lineage.json")
        return Lineage.model_validate_json(p.read_text(encoding="utf-8")) if p.is_file() else None

    # Small named side records, e.g. a submitted batch id that a resumed run must reuse.
    def put_meta(self, key: str, name: str, data: dict[str, Any]) -> None:
        _atomic_write(self.path(key, f".{name}.json"), json.dumps(data, sort_keys=True))

    def delete_meta(self, key: str, name: str) -> None:
        self.path(key, f".{name}.json").unlink(missing_ok=True)

    def get_meta(self, key: str, name: str) -> dict[str, Any] | None:
        p = self.path(key, f".{name}.json")
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
