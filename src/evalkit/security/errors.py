"""The security kit's error type."""

from __future__ import annotations


class SecurityError(ValueError):
    """An attack suite, layer set or run that cannot be used. Messages about a held-out case
    name its manifest id, a count or a file position, never its id or text."""
