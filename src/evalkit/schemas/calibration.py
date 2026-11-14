"""Calibration schemas (SCH-03): hand labels on extracted claims and the held-out report."""

from __future__ import annotations

from typing import Literal

from pydantic import model_validator

from evalkit.schemas._base import (
    Fraction,
    Model,
    NonBlank,
    NonNegInt,
    Timestamp,
    Versioned,
)
from evalkit.schemas.summary import Interval, JudgeAgreement


class CalibrationLabel(Model):
    claim_id: NonBlank
    split: Literal["tuning", "heldout", "relabel"]
    label: Literal["supported", "unsupported"]
    extraction_error: bool = False


class CalibrationReport(Versioned):
    """Held-out agreement for one task under one rubric (Tech Spec §5). Valid only while the
    judge rubric hash is unchanged (CAL-05)."""

    task: NonBlank
    judge_prompt_hash: NonBlank
    scored_at: Timestamp
    n_answers: NonNegInt
    n_claims: NonNegInt
    heldout_digest: NonBlank  # sha256 of the sorted held-out claim ids (CAL-05)
    agreements: tuple[JudgeAgreement, ...]
    extraction_error_rate: Fraction
    self_consistency: Interval | None = None  # owner relabel vs first pass

    @model_validator(mode="after")
    def _consistent(self) -> CalibrationReport:
        for a in self.agreements:
            if a.split != "heldout":
                raise ValueError("a calibration report holds held-out agreement only")
            if a.task != self.task or a.judge_prompt_hash != self.judge_prompt_hash:
                raise ValueError("every agreement must match the report's task and rubric hash")
        names = [a.judge for a in self.agreements]
        if len(names) != len(set(names)):
            raise ValueError("one agreement per judge")
        return self
