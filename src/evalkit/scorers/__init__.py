"""EvalKit scorers. Public API (semver): `from evalkit.scorers import ...`."""

from evalkit.scorers.base import (
    Scorer,
    ScoreResult,
    discover,
    get_scorer,
    scorer,
)
from evalkit.scorers.deterministic import citation_valid, cost, label_correct, latency
from evalkit.scorers.retrieval import mrr, ndcg_at_k, recall_at_k
from evalkit.scorers.sql import sql_match

__all__ = [
    "ScoreResult",
    "Scorer",
    "citation_valid",
    "cost",
    "discover",
    "get_scorer",
    "label_correct",
    "latency",
    "mrr",
    "ndcg_at_k",
    "recall_at_k",
    "scorer",
    "sql_match",
]
