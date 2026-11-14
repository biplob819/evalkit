"""Local datasets for user tasks (CLI-05, OSS-04): JSONL, or CSV exported from a spreadsheet.

One case per line: {"id": "...", "question": "...", "context": ["sentence", ...], "label": ...}.
`label` is optional (unlabelled cases still get groundedness, not accuracy) and, when present,
must be one of the task's labels. Ids must be unique and cannot contain ":" (claim ids use it).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from evalkit.schemas import Case, TaskConfig
from evalkit.text import number_sentences


class DatasetFileError(ValueError):
    pass


def load_jsonl_cases(path: Path, task: TaskConfig) -> list[Case]:
    if not path.is_file():
        raise DatasetFileError(f"dataset file not found: {path}")
    rows: list[tuple[str, object]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append((f"{path}:{i}", json.loads(line)))
        except json.JSONDecodeError as exc:
            raise DatasetFileError(f"{path}:{i}: not valid JSON ({exc.msg})") from None
    return _cases(rows, task, path)


def load_csv_cases(path: Path, task: TaskConfig) -> list[Case]:
    """A spreadsheet export: columns id, question, context and (optionally) label. The context
    cell is the source text; it is split into sentences (each line break also ends one)."""
    if not path.is_file():
        raise DatasetFileError(f"dataset file not found: {path}")
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        header = [h.strip() for h in (reader.fieldnames or [])]
        missing = [k for k in ("id", "question", "context") if k not in header]
        if missing:
            raise DatasetFileError(f"{path}: missing column(s) {', '.join(missing)}")
        rows: list[tuple[str, object]] = []
        for i, raw in enumerate(reader, start=2):  # line 1 is the header
            row = {(k or "").strip(): (v or "") for k, v in raw.items()}
            if not any(str(v).strip() for v in row.values()):
                continue
            text = str(row["context"])
            row["context"] = list(number_sentences(text.splitlines())) if text.strip() else []
            if not str(row.get("label", "")).strip():
                row.pop("label", None)
            rows.append((f"{path}:{i}", row))
    return _cases(rows, task, path)


def _cases(rows: list[tuple[str, object]], task: TaskConfig, path: Path) -> list[Case]:
    labels = {lab.casefold(): lab for lab in task.labels}
    cases: list[Case] = []
    seen: set[str] = set()
    for where, row in rows:
        if not isinstance(row, dict):
            raise DatasetFileError(f"{where}: each line must be a JSON object")
        missing = [k for k in ("id", "question", "context") if k not in row]
        if missing:
            raise DatasetFileError(f"{where}: missing {', '.join(missing)}")
        if not isinstance(row["id"], str | int) or isinstance(row["id"], bool):
            raise DatasetFileError(f"{where}: id must be a string or an integer")
        if not isinstance(row["question"], str) or not row["question"].strip():
            raise DatasetFileError(f"{where}: question must be a non-empty string")
        cid = str(row["id"]).strip()
        if ":" in cid or not cid:
            raise DatasetFileError(f"{where}: id {cid!r} must be non-empty without ':'")
        if cid in seen:
            raise DatasetFileError(f"{where}: duplicate id {cid!r}")
        seen.add(cid)
        context = row["context"]
        if (
            not isinstance(context, list)
            or not context
            or not all(isinstance(s, str) and s.strip() for s in context)
        ):
            raise DatasetFileError(f"{where}: context must be a non-empty list of sentences")
        label = row.get("label")
        if label is not None:
            if str(label).casefold() not in labels:
                raise DatasetFileError(
                    f"{where}: label {label!r} is not one of the task labels {list(task.labels)}"
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
            raise DatasetFileError(f"{where}: invalid case ({exc})") from None
    if not cases:
        raise DatasetFileError(f"{path}: no cases")
    return cases
