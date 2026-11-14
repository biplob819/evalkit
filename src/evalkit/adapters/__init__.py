"""Provider adapters. Import providers lazily: the SDKs are optional extras."""

from evalkit.adapters.base import (
    ALLOWED_PARAMS,
    BatchRequest,
    BatchResult,
    CompletionRequest,
    ModelAdapter,
    RawCompletion,
    discover,
    get_adapter,
    register,
)

__all__ = [
    "ALLOWED_PARAMS",
    "BatchRequest",
    "BatchResult",
    "CompletionRequest",
    "ModelAdapter",
    "RawCompletion",
    "discover",
    "get_adapter",
    "register",
]
