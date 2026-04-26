"""
api/schemas/iteration.py — Pydantic request/response models for iterations.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from api.schemas.trial import TrialSummary


class IterationSummary(BaseModel):
    """Compact iteration view for list pages."""

    model_config = {"from_attributes": True}

    id: str
    iteration_number: int
    status: str
    score: Optional[Decimal]
    outcome: Optional[str]
    started_at: datetime
    finished_at: Optional[datetime]


class IterationDetail(BaseModel):
    """Full iteration detail including optimizer artefacts and trial list."""

    model_config = {"from_attributes": True}

    id: str
    experiment_id: str
    job_id: str
    iteration_number: int
    parent_iteration_id: Optional[str]
    agent_version_id: str
    parent_agent_version_id: Optional[str]
    status: str
    score: Optional[Decimal]
    outcome: Optional[str]
    best_pointer_changed: bool

    optimizer_diagnosis: Optional[str]
    expected_targets: Optional[list[str]]
    optimizer_instructions_uri: Optional[str]
    optimizer_context_uri: Optional[str]
    optimizer_transcript_uri: Optional[str]
    optimizer_input_tokens: Optional[int]
    optimizer_output_tokens: Optional[int]
    optimizer_cost_usd: Optional[Decimal]

    error_message: Optional[str]
    started_at: datetime
    finished_at: Optional[datetime]

    # Populated by the route handler (not on the ORM row).
    trials: list[TrialSummary] = []
