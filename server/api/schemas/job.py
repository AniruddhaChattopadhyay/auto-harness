"""
api/schemas/job.py — Pydantic request/response models for jobs.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class JobCreate(BaseModel):
    """Request body for POST /experiments/{id}/jobs."""

    max_iterations: int = Field(default=5, ge=1, le=50)
    stopping_criteria: Optional[dict] = None


class JobRead(BaseModel):
    """Full job detail."""

    model_config = {"from_attributes": True}

    id: str
    experiment_id: str
    max_iterations: int
    stopping_criteria: dict
    status: str
    started_at_iteration: Optional[int]
    ended_at_iteration: Optional[int]
    stop_reason: Optional[str]
    error_message: Optional[str]
    created_at: datetime
    started_at: Optional[datetime]
    finished_at: Optional[datetime]
