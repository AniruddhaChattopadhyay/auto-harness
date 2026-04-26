"""
api/routes/tasks.py — GET /tasks

Returns the list of TerminalBench tasks supported by a given template.
Requires ?template_id=<id>.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException
from sqlmodel import select

from api.deps import SessionDep
from api.schemas.task import TaskInfo
from core.models import Template

log = structlog.get_logger()

router = APIRouter(tags=["tasks"])


@router.get("/tasks", response_model=list[TaskInfo])
async def list_tasks(
    template_id: str,
    session: SessionDep,
) -> list[TaskInfo]:
    """
    Return the curated TerminalBench task IDs for the given template.

    ``template_id`` is required. Returns 400 if missing (FastAPI enforces this
    as a required query param), 404 if the template doesn't exist.
    """
    result = await session.execute(
        select(Template).where(Template.id == template_id)
    )
    template = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found.")

    task_ids: list[str] = template.default_task_ids or []
    log.info("list_tasks", template_id=template_id, count=len(task_ids))
    return [TaskInfo(task_id=tid, template_id=template_id) for tid in task_ids]
