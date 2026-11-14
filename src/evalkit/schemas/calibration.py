"""Calibration label (SCH-03): one hand label on one extracted claim."""

from __future__ import annotations

from typing import Literal

from evalkit.schemas._base import Model, NonBlank


class CalibrationLabel(Model):
    claim_id: NonBlank
    split: Literal["tuning", "heldout", "relabel"]
    label: Literal["supported", "unsupported"]
    extraction_error: bool = False
