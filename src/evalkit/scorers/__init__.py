"""EvalKit scorers. Public API (semver): `from evalkit.scorers import ...`."""

from evalkit.scorers.attack import attack_outcome
from evalkit.scorers.base import (
    Scorer,
    ScoreResult,
    discover,
    get_scorer,
    scorer,
)
from evalkit.scorers.clarify import ClarificationRates, clarification_rates
from evalkit.scorers.deterministic import citation_valid, cost, label_correct, latency

# D-74: only render_evidence is public in 0.3.0. value_check and its helpers stay in
# evalkit.scorers.evidence (internal) until their strict redesign (B5.16) ships in 0.4.0.
from evalkit.scorers.evidence import render_evidence
from evalkit.scorers.retrieval import mrr, ndcg_at_k, recall_at_k
from evalkit.scorers.rows import answer_from_rows, unsupported_numbers
from evalkit.scorers.rules import Rule, RuleError, RuleSet, rule_compliance, rule_compliance_scorer
from evalkit.scorers.sql import sql_match

__all__ = [
    "ClarificationRates",
    "Rule",
    "RuleError",
    "RuleSet",
    "ScoreResult",
    "Scorer",
    "answer_from_rows",
    "attack_outcome",
    "citation_valid",
    "clarification_rates",
    "cost",
    "discover",
    "get_scorer",
    "label_correct",
    "latency",
    "mrr",
    "ndcg_at_k",
    "recall_at_k",
    "render_evidence",
    "rule_compliance",
    "rule_compliance_scorer",
    "scorer",
    "sql_match",
    "unsupported_numbers",
]
