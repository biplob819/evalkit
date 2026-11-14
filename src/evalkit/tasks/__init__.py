"""Built-in task definitions and prompt rendering."""

from __future__ import annotations

from pathlib import Path

from evalkit.config import load_task_config
from evalkit.schemas import Case, TaskConfig

TASKS_DIR = Path(__file__).parent


def builtin_task(name: str) -> TaskConfig:
    path = TASKS_DIR / f"{name}.yaml"
    if not path.is_file():
        available = sorted(p.stem for p in TASKS_DIR.glob("*.yaml"))
        raise ValueError(f"unknown built-in task {name!r}; available: {available}")
    return load_task_config(path)


def render_prompt(task: TaskConfig, case: Case) -> str:
    """Fill the task's template. Sentences are shown as `[i] text`, numbered from 1."""
    numbered = "\n".join(f"[{i}] {s}" for i, s in enumerate(case.context, start=1))
    return task.prompt_template.format(
        question=case.question,
        numbered_context=numbered,
        labels=" / ".join(task.labels),
        n_sentences=len(case.context),
    )


__all__ = ["TASKS_DIR", "builtin_task", "render_prompt"]
