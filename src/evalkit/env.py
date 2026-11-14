"""Provider keys from the environment or a git-ignored .env file. Values are never printed."""

from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def parse_dotenv_line(line: str) -> tuple[str, str] | None:
    """KEY=VALUE, optional `export ` prefix, single or double quotes, inline # comments."""
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export ") :].lstrip()
    key, value = line.split("=", 1)
    key, value = key.strip(), value.strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        value = value[1:end] if end != -1 else value[1:]
    else:
        value = value.split(" #", 1)[0].strip()
    return key, value


def load_dotenv(path: Path) -> None:
    """Minimal .env loader; existing environment variables win. Values are never printed."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = parse_dotenv_line(line)
        if parsed:
            os.environ.setdefault(*parsed)


def missing_keys(providers: Iterable[str]) -> list[str]:
    """Names (never values) of provider keys that are not set."""
    return sorted(KEYS[p] for p in set(providers) if p in KEYS and not os.environ.get(KEYS[p]))
