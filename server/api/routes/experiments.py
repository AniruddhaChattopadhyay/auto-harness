"""
api/routes/experiments.py — experiment CRUD + iteration listing endpoints.

Routes:
  POST   /experiments
  GET    /experiments
  GET    /experiments/{id}
  GET    /experiments/{id}/iterations
  GET    /experiments/{id}/iterations/{n}
"""

from __future__ import annotations

import asyncio
import hashlib
from typing import Optional

import structlog
from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import func, text
from sqlmodel import select

from api.deps import SessionDep, not_found
from api.schemas.experiment import ExperimentCreate, ExperimentRead, ExperimentSummary
from api.schemas.iteration import IterationDetail, IterationSummary
from api.schemas.trial import TrialSummary
from core.models import AgentVersion, Experiment, Iteration, Template, Trial
from core.storage import get_object_text, put_object

log = structlog.get_logger()

router = APIRouter(tags=["experiments"])


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _get_experiment_or_404(session: SessionDep, experiment_id: str) -> Experiment:
    result = await session.execute(
        select(Experiment).where(Experiment.id == experiment_id)
    )
    exp = result.scalar_one_or_none()
    if exp is None:
        not_found("Experiment", experiment_id)
    return exp  # type: ignore[return-value]


async def _iteration_count(session: SessionDep, experiment_id: str) -> int:
    result = await session.execute(
        text("SELECT COUNT(*) FROM iteration WHERE experiment_id = :eid"),
        {"eid": experiment_id},
    )
    return result.scalar_one()


def _build_experiment_read(exp: Experiment, count: int) -> ExperimentRead:
    read = ExperimentRead.model_validate(exp)
    read.current_iteration_count = count
    return read


# ── POST /experiments ──────────────────────────────────────────────────────────


@router.post("/experiments", status_code=201, response_model=ExperimentRead)
async def create_experiment(
    body: ExperimentCreate,
    session: SessionDep,
    response: Response,
) -> ExperimentRead:
    """
    Create a new experiment.

    Resolution order (§5.16):
      1. body fields win
      2. template defaults fill any missing field
    """
    # ── 1. Fetch template (404 if missing) ────────────────────────────────────
    result = await session.execute(
        select(Template).where(Template.id == body.template_id)
    )
    template: Optional[Template] = result.scalar_one_or_none()
    if template is None:
        raise HTTPException(status_code=404, detail=f"Template '{body.template_id}' not found.")

    # ── 2. Resolve fields using body > template default ───────────────────────
    task_ids = body.task_ids if body.task_ids is not None else template.default_task_ids
    test_task_ids = body.test_task_ids if body.test_task_ids is not None else template.default_test_task_ids
    optimizer_model = body.optimizer_model or template.default_optimizer_model or "claude-sonnet-4-6"
    optimizer_kind = body.optimizer_kind or template.default_optimizer_kind
    max_concurrency = body.max_concurrency if body.max_concurrency is not None else template.default_max_concurrency
    sandbox_provider = body.sandbox_provider or template.default_sandbox_provider

    # ── 3. Obtain baseline agent .py source ───────────────────────────────────
    if body.baseline_agent_py is not None:
        agent_source = body.baseline_agent_py
    else:
        # Fetch from MinIO (sync call → wrap in thread)
        try:
            agent_source = await asyncio.to_thread(
                get_object_text, template.default_baseline_agent_uri
            )
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail=f"Failed to fetch baseline agent from MinIO: {exc}",
            )

    # ── 4. Build experiment row (to get the ID for workspace_uri) ─────────────
    exp = Experiment(
        name=body.name,
        description=body.description,
        template_id=body.template_id,
        task_ids=task_ids,
        test_task_ids=test_task_ids,
        optimizer_model=optimizer_model,
        optimizer_kind=optimizer_kind,
        max_concurrency=max_concurrency,
        sandbox_provider=sandbox_provider,
        workspace_uri="",  # filled below once we have the ID
        status="created",
    )
    session.add(exp)
    await session.flush()  # assigns exp.id

    workspace_uri = f"experiments/{exp.id}/"
    exp.workspace_uri = workspace_uri

    # ── 5. Create AgentVersion v0 ─────────────────────────────────────────────
    source_bytes = agent_source.encode("utf-8") if isinstance(agent_source, str) else agent_source
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    source_uri = f"{workspace_uri}agents/v0.py"

    av = AgentVersion(
        experiment_id=exp.id,
        version_number=0,
        parent_id=None,
        source_uri=source_uri,
        source_hash=source_hash,
    )
    session.add(av)
    await session.flush()  # assigns av.id

    # ── 6. Upload baseline .py to MinIO (sync → thread) ───────────────────────
    try:
        await asyncio.to_thread(
            put_object, source_uri, source_bytes, "text/x-python"
        )
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to upload baseline agent to MinIO: {exc}",
        )

    # ── 7. Wire experiment back-pointers ──────────────────────────────────────
    exp.baseline_agent_version_id = av.id
    exp.best_agent_version_id = av.id

    # Session commits via SessionDep on clean exit.
    log.info(
        "experiment_created",
        experiment_id=exp.id,
        template_id=body.template_id,
        agent_version_id=av.id,
    )

    return _build_experiment_read(exp, 0)


# ── GET /experiments ───────────────────────────────────────────────────────────


@router.get("/experiments", response_model=list[ExperimentSummary])
async def list_experiments(
    session: SessionDep,
    limit: int = Query(default=50, ge=1, le=200),
    before: Optional[str] = Query(default=None, description="Cursor: return experiments with id < before"),
) -> list[ExperimentSummary]:
    """
    Paginated list of experiments.

    Uses ``before=<id>`` cursor (ULID lexicographic order → newest-first pagination).
    """
    stmt = select(Experiment).order_by(Experiment.id.desc()).limit(limit)  # type: ignore[union-attr]
    if before:
        stmt = stmt.where(Experiment.id < before)

    result = await session.execute(stmt)
    experiments = result.scalars().all()
    return [ExperimentSummary.model_validate(e) for e in experiments]


# ── GET /experiments/{id} ──────────────────────────────────────────────────────


@router.get("/experiments/{experiment_id}", response_model=ExperimentRead)
async def get_experiment(
    experiment_id: str,
    session: SessionDep,
) -> ExperimentRead:
    exp = await _get_experiment_or_404(session, experiment_id)
    count = await _iteration_count(session, experiment_id)
    log.info("get_experiment", experiment_id=experiment_id)
    return _build_experiment_read(exp, count)


# ── GET /experiments/{id}/iterations ──────────────────────────────────────────


@router.get("/experiments/{experiment_id}/iterations", response_model=list[IterationSummary])
async def list_iterations(
    experiment_id: str,
    session: SessionDep,
) -> list[IterationSummary]:
    """List all iterations for an experiment, ordered by iteration_number."""
    await _get_experiment_or_404(session, experiment_id)

    result = await session.execute(
        select(Iteration)
        .where(Iteration.experiment_id == experiment_id)
        .order_by(Iteration.iteration_number)  # type: ignore[union-attr]
    )
    iterations = result.scalars().all()
    log.info("list_iterations", experiment_id=experiment_id, count=len(iterations))
    return [IterationSummary.model_validate(it) for it in iterations]


# ── GET /experiments/{id}/iterations/{n} ──────────────────────────────────────


@router.get(
    "/experiments/{experiment_id}/iterations/{iteration_number}",
    response_model=IterationDetail,
)
async def get_iteration(
    experiment_id: str,
    iteration_number: int,
    session: SessionDep,
) -> IterationDetail:
    """Full iteration detail including all trials."""
    await _get_experiment_or_404(session, experiment_id)

    iter_result = await session.execute(
        select(Iteration)
        .where(Iteration.experiment_id == experiment_id)
        .where(Iteration.iteration_number == iteration_number)
    )
    iteration = iter_result.scalar_one_or_none()
    if iteration is None:
        raise HTTPException(
            status_code=404,
            detail=f"Iteration {iteration_number} not found for experiment '{experiment_id}'.",
        )

    # Fetch trials for this iteration.
    trials_result = await session.execute(
        select(Trial).where(Trial.iteration_id == iteration.id)
    )
    trials = trials_result.scalars().all()
    trial_summaries = [TrialSummary.model_validate(t) for t in trials]

    detail = IterationDetail.model_validate(iteration)
    detail.trials = trial_summaries

    log.info(
        "get_iteration",
        experiment_id=experiment_id,
        iteration_number=iteration_number,
        trial_count=len(trials),
    )
    return detail
