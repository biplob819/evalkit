"""Which adapter serves a model (since 0.4.0, D-61 rule 2: ai-visibility and
fraud-review-bedrock). `provider_for` is the object EvalKit's own run uses."""

from evalkit.pipeline.generate import provider_for

__all__ = ["provider_for"]
