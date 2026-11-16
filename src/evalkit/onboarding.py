"""Onboarding commands (B2.4): `evalkit init`, `evalkit doctor`, `evalkit report --example`."""

from __future__ import annotations

import importlib.util
import json
import os
import platform
import socket
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from evalkit.env import KEYS

EXAMPLE_DIR = Path(__file__).parent / "examples" / "example_run"
PRICES_MAX_AGE_DAYS = 90
HOSTS = {"anthropic": "api.anthropic.com", "openai": "api.openai.com"}

CONFIG_TEMPLATE = """\
# EvalKit run config. Docs: https://github.com/biplob819/evalkit#readme
# Models under test (two pairs at matching list prices).
models: [claude-sonnet-5-5, claude-haiku-5-5, gpt-6.1-sol, gpt-6-luna]
# Two judges from different families check every claim (one is enough to start).
judges: [claude-opus-5-5, gpt-6.1-sol]
extractor: claude-haiku-5-5
seed: 7
max_cost_usd: 20          # total for a run folder, across resumes
default_effort: medium
judge_effort: medium
extractor_effort: medium
thresholds:
  - {metric: headline, op: ">=", value: 0.6}
# Local or self-hosted OpenAI-compatible models:
# endpoints: {my-llama: http://localhost:8000/v1}
"""

ENV_TEMPLATE = """\
# Copy to .env and fill in. .env is git-ignored; EvalKit never prints key values.
ANTHROPIC_API_KEY=
OPENAI_API_KEY=
"""

TASK_TEMPLATE = """\
# Your own task. Each line of my_task.jsonl is one case:
#   {"id": "...", "question": "...", "context": ["sentence 1", "sentence 2"], "label": "yes"}
name: my-task
dataset: my_task.jsonl
split: local
labels: ["yes", "no", "maybe"]
output_schema: answer_v1
scorers: [label_correct, citation_valid, groundedness, coverage, cost, latency]
prompt_template: |
  Answer the question using only the numbered sentences below.

  Question: {question}

  Sentences:
  {numbered_context}

  Reply with JSON only, in this shape:
  {{"label": one of {labels}, "answer": "two to four sentences", "citations": [sentence numbers]}}

  Rules:
  - Base every statement on the sentences above, not on outside knowledge.
  - Cite the sentence numbers (1 to {n_sentences}) that support your answer.
  - Choose "maybe" when the sentences do not settle the question.
"""

TASK_CASES = [
    {
        "id": "case-1",
        "question": "Did the new triage process shorten waiting times?",
        "context": [
            "The clinic introduced a nurse-led triage process in March.",
            "Median waiting time fell from 52 to 31 minutes over the following six months.",
            "Patient satisfaction scores were unchanged.",
        ],
        "label": "yes",
    },
    {
        "id": "case-2",
        "question": "Did the reminder letters increase screening uptake?",
        "context": [
            "Reminder letters were sent to 2,000 patients due for screening.",
            "Uptake was 41% in the reminder group and 40% in the control group.",
            "The difference was not statistically significant.",
        ],
        "label": "no",
    },
    {
        "id": "case-3",
        "question": "Does the app improve medication adherence in older adults?",
        "context": [
            "Twelve adults over 70 used the app for two weeks.",
            "Most reported that the reminders were helpful.",
            "Adherence was not measured objectively.",
        ],
        "label": "maybe",
    },
]

GITIGNORE_LINES = (".env", "runs/", ".evalkit/")


@dataclass
class InitResult:
    written: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def init_project(root: Path, *, force: bool = False) -> InitResult:
    """Scaffold a project. Existing files are kept; `force` regenerates only `evalkit.yaml` and
    `.env.example` (never the task files, which hold the user's data); `.gitignore` is only
    ever appended to."""
    if root.exists() and not root.is_dir():
        raise ValueError(f"{root} exists and is not a folder")
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "evalkit.yaml": CONFIG_TEMPLATE,
        ".env.example": ENV_TEMPLATE,
        "tasks/my_task.yaml": TASK_TEMPLATE,
        "tasks/my_task.jsonl": "".join(json.dumps(c) + "\n" for c in TASK_CASES),
    }
    result = InitResult()
    for rel, text in files.items():
        path = root / rel
        # tasks/* hold the user's own cases once edited: never overwritten, even with --force.
        if path.exists() and (not force or rel.startswith("tasks/")):
            result.skipped.append(rel)
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        result.written.append(rel)
    gi = root / ".gitignore"
    existing = gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else []
    missing = [line for line in GITIGNORE_LINES if line not in existing]
    if missing:
        with gi.open("a", encoding="utf-8") as fh:
            if existing and existing[-1].strip():
                fh.write("\n")
            fh.write("# EvalKit\n" + "".join(f"{line}\n" for line in missing))
        result.written.append(".gitignore")
    return result


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    blocking: bool = False


def _reachable(host: str, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            return True
    except OSError:
        return False


def prices_age_days(today: date | None = None) -> int | None:
    import yaml

    from evalkit.cost import PRICES_YAML

    data = yaml.safe_load(PRICES_YAML.read_text(encoding="utf-8")) or {}
    try:
        stamps = [
            v["checked_on"]
            if isinstance(v["checked_on"], date)
            else date.fromisoformat(str(v["checked_on"]))
            for v in data.values()
            if isinstance(v, dict) and v.get("checked_on")
        ]
    except ValueError:
        return None  # a malformed date is reported as missing, not a crash
    if not stamps:
        return None
    return ((today or date.today()) - min(stamps)).days  # the stalest price decides


def _dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


SDK_MODULE = {"anthropic": "anthropic", "openai": "openai", "openai_compatible": "openai"}


def needed_providers(config_path: Path | None) -> set[str] | None:
    """Providers a config needs (models, extractor, at least one judge); None without one."""
    if config_path is None or not config_path.is_file():
        return None
    from evalkit.config import load_run_config
    from evalkit.pipeline.generate import provider_for

    config = load_run_config(config_path)
    names = (*config.models, config.extractor)
    return {provider_for(m, config.endpoints, config.systems) for m in names}


def doctor(
    *,
    offline: bool = False,
    store: Path = Path(".evalkit/store"),
    config: Path | None = Path("evalkit.yaml"),
) -> list[Check]:
    """Readiness checks. Key values are never read into the output, only whether they exist.
    With a config, every provider it needs must have its key and SDK; without one, any provider
    whose key is set must have its SDK, and at least one key must be set."""
    checks: list[Check] = []
    py_ok = sys.version_info >= (3, 11)
    checks.append(Check("python", py_ok, platform.python_version(), blocking=not py_ok))
    try:
        needed = needed_providers(config)
    except (ValueError, OSError) as exc:
        needed = None
        checks.append(Check("config", False, f"{config} is invalid: {exc}", blocking=True))
    else:
        if needed is not None:
            checks.append(Check("config", True, f"{config} needs {', '.join(sorted(needed))}"))
    set_keys = {p: bool(os.environ.get(var)) for p, var in KEYS.items()}
    for provider, module in (("anthropic", "anthropic"), ("openai", "openai")):
        found = importlib.util.find_spec(module) is not None
        required = provider in needed if needed is not None else set_keys[provider]
        checks.append(
            Check(
                f"sdk:{provider}",
                found,
                "installed" if found else f"missing: pip install 'evalkit-harness[{provider}]'",
                blocking=required and not found,
            )
        )
    for provider, var in KEYS.items():
        required = needed is not None and provider in needed
        state = "set" if set_keys[provider] else "not set"
        checks.append(
            Check(
                f"key:{provider}",
                set_keys[provider],
                f"{var} {state}" + (" (needed by the config)" if required else ""),
                blocking=required and not set_keys[provider],
            )
        )
    if not any(set_keys.values()) and needed is None:
        checks.append(
            Check("keys", False, "no provider key set: copy .env.example to .env", blocking=True)
        )
    if offline:
        checks.append(Check("network", True, "skipped (--offline)"))
    else:
        for provider, host in HOSTS.items():
            reach = _reachable(host)
            checks.append(
                Check(
                    f"network:{provider}",
                    reach,
                    f"{host} {'reachable' if reach else 'unreachable'}",
                )
            )
    age = prices_age_days()
    if age is None:
        checks.append(Check("prices", False, "prices.yaml has no date"))
    else:
        fresh = age <= PRICES_MAX_AGE_DAYS
        checks.append(
            Check(
                "prices",
                fresh,
                f"prices.yaml is {age} days old"
                + ("" if fresh else f" (older than {PRICES_MAX_AGE_DAYS}; check provider pricing)"),
            )
        )
    size = _dir_size(store)
    checks.append(Check("cache", True, f"{store} holds {size / 1e6:.1f} MB"))
    return checks
