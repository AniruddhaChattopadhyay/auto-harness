"""
api/schemas/experiment.py — Pydantic request/response models for experiments.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class ExperimentCreate(BaseModel):
    """Request body for POST /experiments."""

    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = None
    template_id: str  # FK to templates

    # Optional overrides; if None, copied from template defaults.
    task_ids: Optional[list[str]] = None
    test_task_ids: Optional[list[str]] = None  # subset hidden from optimizer (train/test holdout)
    optimizer_model: Optional[str] = None
    optimizer_kind: Optional[str] = None
    max_concurrency: Optional[int] = None
    sandbox_provider: Optional[str] = None

    # Inline source of the baseline agent .py.
    # If None → fetch from template.default_baseline_agent_uri in MinIO.
    baseline_agent_py: Optional[str] = None


class ExperimentRead(BaseModel):
    """Full experiment detail returned by POST /experiments and GET /experiments/{id}."""

    model_config = {"from_attributes": True}

    id: str
    name: str
    description: Optional[str]
    template_id: str

    task_ids: Optional[list[str]]
    test_task_ids: Optional[list[str]] = None
    optimizer_kind: str
    optimizer_model: str
    sandbox_provider: str
    max_concurrency: int
    workspace_uri: str

    baseline_agent_version_id: Optional[str]
    best_agent_version_id: Optional[str]
    best_score: Optional[Decimal]
    status: str

    # Derived: count of iteration rows for this experiment.
    current_iteration_count: int = 0

    created_at: datetime
    updated_at: datetime


class ExperimentSummary(BaseModel):
    """Compact experiment row for list pages."""

    model_config = {"from_attributes": True}

    id: str
    name: str
    status: str
    best_score: Optional[Decimal]
    created_at: datetime
