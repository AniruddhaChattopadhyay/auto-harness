"""
api/schemas/task.py — Pydantic response model for GET /tasks.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class TaskInfo(BaseModel):
    """Information about one TerminalBench task our service supports."""

    task_id: str
    template_id: str
