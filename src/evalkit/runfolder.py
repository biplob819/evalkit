"""What makes a folder an EvalKit run folder (0.4.0; strict detection, internal).

`run.json` and `summary.json` are common names: other projects write their own (agentic-analytics'
results folders hold both, with `schema_version: "1.0"` like EvalKit's). A file is EvalKit's only
when it carries EvalKit's marker: a JSON object whose `schema_version` is EvalKit's results
format (`<major>.<minor>`, 1.x today) and which has every key EvalKit always writes to that file
(`RUN_KEYS`, `SUMMARY_KEYS`). Anything else is not an EvalKit run, and every reader says why
instead of reading it as one. A file that carries the marker but fails EvalKit's schema is a
broken EvalKit file and is reported as such by the reader.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from evalkit.schemas._base import VERSION_PATTERN

__all__: list[str] = []  # internal: the rule is documented in docs/compare.md

# Keys EvalKit writes to every run.json (RunManifest) and summary.json (RunSummary) since 0.1;
# none is optional, and together they are what a project's own file does not have.
RUN_KEYS = ("run_id", "task", "task_ref", "config_hash", "judge_mode", "judges", "live_judges")
SUMMARY_KEYS = (
    "run_id",
    "task",
    "config_hash",
    "judge_mode",
    "calibration_status",
    "judging_status",
    "models",
)
_VERSION = re.compile(VERSION_PATTERN)
FileName = Literal["run.json", "summary.json"]


def marker_problem(data: object, name: FileName) -> str | None:
    """None when parsed JSON `data` carries EvalKit's marker for `name`; else why not (safe to
    print: it names keys and EvalKit's own expectations, never the file's values)."""
    if not isinstance(data, dict):
        return "is not a JSON object"
    version = data.get("schema_version")
    if not isinstance(version, str) or not _VERSION.match(version):
        return "has no EvalKit schema_version (EvalKit writes '<major>.<minor>', e.g. '1.0')"
    keys = RUN_KEYS if name == "run.json" else SUMMARY_KEYS
    missing = [k for k in keys if k not in data]
    if missing:
        return (
            f"has schema_version {version!r} but not EvalKit's {name} keys "
            f"({', '.join(missing)}): another project's {name}, not an EvalKit run"
        )
    return None


def file_problem(path: Path) -> str | None:
    """`marker_problem` for a file on disk; a file that cannot be read or parsed is not
    EvalKit's either."""
    name: FileName = "run.json" if path.name == "run.json" else "summary.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        return f"cannot be read ({type(exc).__name__})"
    except ValueError:
        return "is not valid JSON"
    return marker_problem(data, name)


def is_evalkit_file(path: Path) -> bool:
    return path.is_file() and file_problem(path) is None
