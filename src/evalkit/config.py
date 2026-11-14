"""Load run and task configuration from YAML with errors a user can act on."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from evalkit.schemas import RunConfig, TaskConfig

M = TypeVar("M", bound=BaseModel)


class ConfigError(ValueError):
    """A config file is missing, unreadable or invalid. The message names file and field."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys instead of keeping the last one."""


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    seen: set[Any] = set()
    for key_node, _ in node.value:
        key = loader.construct_object(key_node, deep=True)  # type: ignore[arg-type]
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None, None, f"duplicate key {key!r}", key_node.start_mark
            )
        seen.add(key)
    return loader.construct_mapping(node, deep=True)


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,  # type: ignore[arg-type]
)


def _load(path: str | Path, model: type[M]) -> M:
    p = Path(path)
    if p.is_dir():
        raise ConfigError(f"{p}: is a directory, expected a YAML file")
    if not p.exists():
        raise ConfigError(f"{p}: file not found")
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{p}: not UTF-8 text ({exc.reason} at byte {exc.start})") from exc
    except OSError as exc:
        raise ConfigError(f"{p}: cannot read file: {exc.strerror or exc}") from exc
    try:
        data = yaml.load(text, Loader=_UniqueKeyLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{p}: not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{p}: expected a mapping at the top level")
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "(top level)"
            problems.append(f"  {loc}: {err['msg']}")
        raise ConfigError(f"{p}: invalid {model.__name__}\n" + "\n".join(problems)) from exc


def load_run_config(path: str | Path) -> RunConfig:
    return _load(path, RunConfig)


def load_task_config(path: str | Path) -> TaskConfig:
    return _load(path, TaskConfig)
