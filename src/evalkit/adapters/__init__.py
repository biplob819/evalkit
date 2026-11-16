"""Provider adapters. Import providers lazily: the SDKs are optional extras."""

from evalkit.adapters.base import (
    ALLOWED_PARAMS,
    BatchRequest,
    BatchResult,
    CompletionRequest,
    ModelAdapter,
    RawCompletion,
    SystemAdapter,
    discover,
    discover_systems,
    get_adapter,
    get_system,
    register,
    register_system,
)

__all__ = [
    "ALLOWED_PARAMS",
    "BatchRequest",
    "BatchResult",
    "CompletionRequest",
    "ModelAdapter",
    "RawCompletion",
    "SystemAdapter",
    "discover",
    "discover_systems",
    "get_adapter",
    "get_system",
    "register",
    "register_system",
]
