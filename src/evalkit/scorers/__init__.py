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

# value_check and its helpers were withheld from 0.3.0 (D-74); public again since 0.4.0 as the
# strict structured-facts check (B5.16, D-77).
from evalkit.scorers.evidence import (
    ValueCheckShares,
    ValuePhrase,
    render_evidence,
    value_check,
    value_check_scorer,
    value_check_shares,
)
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
    "ValueCheckShares",  # 0.4.0 (B5.16, D-77)
    "ValuePhrase",  # 0.4.0 (B5.16, D-77)
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
    "value_check",  # 0.4.0 (B5.16, D-77)
    "value_check_scorer",  # 0.4.0 (B5.16, D-77)
    "value_check_shares",  # 0.4.0 (B5.16, D-77)
]
