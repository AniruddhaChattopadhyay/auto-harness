"""
api/routes/jobs.py — job management endpoints.

Routes:
  POST   /experiments/{id}/jobs
  GET    /experiments/{id}/jobs
  GET    /experiments/{id}/jobs/{job_id}
  POST   /experiments/{id}/jobs/{job_id}/cancel
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy import text
from sqlmodel import select

from api.deps import SessionDep, not_found
from api.schemas.job import JobCreate, JobRead
from core.models import Experiment, Iteration, Job
from core.queue import enqueue

log = structlog.get_logger()

router = APIRouter(tags=["jobs"])

# Default stopping criteria applied when the caller doesn't supply their own.
_DEFAULT_STOPPING_CRITERIA = {
    "perfect_score_stop": True,
    "stagnation_threshold": 2,
    "wall_time_seconds": 5400,
}


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _get_experiment_or_404(session: SessionDep, experiment_id: str) -> Experiment:
    result = await session.execute(
        select(Experiment).where(Experiment.id == experiment_id)
    )
    exp = result.scalar_one_or_none()
    if exp is None:
        not_found("Experiment", experiment_id)
    return exp  # type: ignore[return-value]


async def _get_job_or_404(
    session: SessionDep,
    experiment_id: str,
    job_id: str,
) -> Job:
    result = await session.execute(
        select(Job)
        .where(Job.id == job_id)
        .where(Job.experiment_id == experiment_id)
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(
            status_code=404,
            detail=f"Job '{job_id}' not found for experiment '{experiment_id}'.",
        )
    return job


# ── POST /experiments/{id}/jobs ────────────────────────────────────────────────


@router.post(
    "/experiments/{experiment_id}/jobs",
    status_code=202,
    response_model=JobRead,
)
async def create_job(
    experiment_id: str,
    body: JobCreate,
    session: SessionDep,
) -> JobRead:
    """
    Start a new optimization job within an experiment.

    409 if there is already a queued or running job for this experiment.
    """
    exp = await _get_experiment_or_404(session, experiment_id)

    stopping_criteria = body.stopping_criteria or _DEFAULT_STOPPING_CRITERIA

    # ── Insert job row ─────────────────────────────────────────────────────────
    job = Job(
        experiment_id=experiment_id,
        max_iterations=body.max_iterations,
        stopping_criteria=stopping_criteria,
        status="queued",
    )
    session.add(job)

    try:
        await session.flush()  # will raise IntegrityError if partial-unique violated
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Experiment '{experiment_id}' already has a queued or running job.",
        )

    # ── Decide what to enqueue ─────────────────────────────────────────────────
    if exp.status == "created":
        # First-ever job: baseline run (iteration 0).
        iteration = Iteration(
            experiment_id=experiment_id,
            job_id=job.id,
            iteration_number=0,
            agent_version_id=exp.baseline_agent_version_id,  # type: ignore[arg-type]
            status="running",
            outcome=None,
        )
        session.add(iteration)
        await session.flush()  # assign iteration.id

        exp.status = "baselining"

        await enqueue(session, "benchmark.run", {"iteration_id": iteration.id})

        log.info(
            "job_created_baseline",
            experiment_id=experiment_id,
            job_id=job.id,
            iteration_id=iteration.id,
        )

    else:
        # Subsequent job: find the last finished iteration and enqueue optimizer.propose.
        last_iter_result = await session.execute(
            text(
                """
                SELECT id
                FROM   iteration
                WHERE  experiment_id = :eid
                  AND  status        = 'done'
                ORDER  BY iteration_number DESC
                LIMIT  1
                """
            ),
            {"eid": experiment_id},
        )
        last_row = last_iter_result.fetchone()

        if last_row is None:
            # No finished iterations yet (e.g. baseline still running).
            # Enqueue optimizer.propose against the baseline iteration if it exists.
            any_iter_result = await session.execute(
                text(
                    """
                    SELECT id FROM iteration
                    WHERE  experiment_id = :eid
                    ORDER  BY iteration_number DESC
                    LIMIT  1
                    """
                ),
                {"eid": experiment_id},
            )
            any_row = any_iter_result.fetchone()
            if any_row:
                last_iteration_id = any_row[0]
            else:
                # No iterations at all — treat like a first job.
                iteration = Iteration(
                    experiment_id=experiment_id,
                    job_id=job.id,
                    iteration_number=0,
                    agent_version_id=exp.baseline_agent_version_id,  # type: ignore[arg-type]
                    status="running",
                    outcome=None,
                )
                session.add(iteration)
                await session.flush()
                exp.status = "baselining"
                await enqueue(session, "benchmark.run", {"iteration_id": iteration.id})
                log.info(
                    "job_created_baseline_fallback",
                    experiment_id=experiment_id,
                    job_id=job.id,
                    iteration_id=iteration.id,
                )
                return JobRead.model_validate(job)

            last_iteration_id = last_iteration_id
        else:
            last_iteration_id = last_row[0]

        await enqueue(
            session,
            "optimizer.propose",
            {"iteration_id": last_iteration_id, "job_id": job.id},
        )

        log.info(
            "job_created_optimizer_propose",
            experiment_id=experiment_id,
            job_id=job.id,
            last_iteration_id=last_iteration_id,
        )

    return JobRead.model_validate(job)


# ── GET /experiments/{id}/jobs ─────────────────────────────────────────────────


@router.get("/experiments/{experiment_id}/jobs", response_model=list[JobRead])
async def list_jobs(
    experiment_id: str,
    session: SessionDep,
) -> list[JobRead]:
    """List all jobs for an experiment, newest first."""
    await _get_experiment_or_404(session, experiment_id)

    result = await session.execute(
        select(Job)
        .where(Job.experiment_id == experiment_id)
        .order_by(Job.created_at.desc())  # type: ignore[union-attr]
    )
    jobs = result.scalars().all()
    return [JobRead.model_validate(j) for j in jobs]


# ── GET /experiments/{id}/jobs/{job_id} ────────────────────────────────────────


@router.get(
    "/experiments/{experiment_id}/jobs/{job_id}",
    response_model=JobRead,
)
async def get_job(
    experiment_id: str,
    job_id: str,
    session: SessionDep,
) -> JobRead:
    job = await _get_job_or_404(session, experiment_id, job_id)
    log.info("get_job", experiment_id=experiment_id, job_id=job_id)
    return JobRead.model_validate(job)


# ── POST /experiments/{id}/jobs/{job_id}/cancel ────────────────────────────────


@router.post(
    "/experiments/{experiment_id}/jobs/{job_id}/cancel",
    response_model=JobRead,
)
async def cancel_job(
    experiment_id: str,
    job_id: str,
    session: SessionDep,
) -> JobRead:
    """
    Cancel a job (design.md §5.13 — passive cancellation only).

    Idempotent: if already in a terminal state, returns 200 with the current status.
    """
    job = await _get_job_or_404(session, experiment_id, job_id)

    if job.status in ("queued", "running"):
        job.status = "cancelled"
        log.info("job_cancelled", experiment_id=experiment_id, job_id=job_id)

    return JobRead.model_validate(job)
