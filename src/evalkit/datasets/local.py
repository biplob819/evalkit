"""Local JSONL datasets for user tasks (CLI-05).

One case per line: {"id": "...", "question": "...", "context": ["sentence", ...], "label": ...}.
`label` is optional (unlabelled cases still get groundedness, not accuracy) and, when present,
must be one of the task's labels. Ids must be unique and cannot contain ":" (claim ids use it).
"""

from __future__ import annotations

import json
from pathlib import Path

from evalkit.schemas import Case, TaskConfig


class DatasetFileError(ValueError):
    pass


def load_jsonl_cases(path: Path, task: TaskConfig) -> list[Case]:
    if not path.is_file():
        raise DatasetFileError(f"dataset file not found: {path}")
    labels = {lab.casefold(): lab for lab in task.labels}
    cases: list[Case] = []
    seen: set[str] = set()
    for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DatasetFileError(f"{path}:{i}: not valid JSON ({exc.msg})") from None
        if not isinstance(row, dict):
            raise DatasetFileError(f"{path}:{i}: each line must be a JSON object")
        missing = [k for k in ("id", "question", "context") if k not in row]
        if missing:
            raise DatasetFileError(f"{path}:{i}: missing {', '.join(missing)}")
        if not isinstance(row["id"], str | int) or isinstance(row["id"], bool):
            raise DatasetFileError(f"{path}:{i}: id must be a string or an integer")
        if not isinstance(row["question"], str) or not row["question"].strip():
            raise DatasetFileError(f"{path}:{i}: question must be a non-empty string")
        cid = str(row["id"])
        if ":" in cid or not cid.strip():
            raise DatasetFileError(f"{path}:{i}: id {cid!r} must be non-empty without ':'")
        if cid in seen:
            raise DatasetFileError(f"{path}:{i}: duplicate id {cid!r}")
        seen.add(cid)
        context = row["context"]
        if (
            not isinstance(context, list)
            or not context
            or not all(isinstance(s, str) and s.strip() for s in context)
        ):
            raise DatasetFileError(f"{path}:{i}: context must be a non-empty list of sentences")
        label = row.get("label")
        if label is not None:
            if str(label).casefold() not in labels:
                raise DatasetFileError(
                    f"{path}:{i}: label {label!r} is not one of the task labels {list(task.labels)}"
                )
            label = labels[str(label).casefold()]
        try:
            cases.append(
                Case(
                    id=cid,
                    question=row["question"],
                    context=tuple(context),
                    reference_label=label,
                    source=f"local/{task.name}",
                )
            )
        except ValueError as exc:  # pydantic ValidationError is a ValueError
            raise DatasetFileError(f"{path}:{i}: invalid case ({exc})") from None
    if not cases:
        raise DatasetFileError(f"{path}: no cases")
    return cases
