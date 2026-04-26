"""
api/schemas/trial.py — Pydantic request/response models for trials.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class TrialSummary(BaseModel):
    """Compact trial view used inside IterationDetail."""

    model_config = {"from_attributes": True}

    id: str
    task_id: str
    status: str
    reward: Optional[Decimal]
    wall_time_ms: Optional[int]


class TrialDetail(BaseModel):
    """Full trial detail including MinIO blob URIs."""

    model_config = {"from_attributes": True}

    id: str
    iteration_id: str
    task_id: str
    status: str
    reward: Optional[Decimal]
    wall_time_ms: Optional[int]
    command_count: Optional[int]
    sandbox_id: Optional[str]
    trace_uri: Optional[str]
    verifier_output_uri: Optional[str]
    infra_error: Optional[str]
    started_at: datetime
    finished_at: Optional[datetime]
