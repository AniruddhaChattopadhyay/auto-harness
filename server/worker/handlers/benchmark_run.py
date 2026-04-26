"""
worker/handlers/benchmark_run.py — Handler for benchmark.run queue tasks.

See design.md §5.11 and the deliverable description.

Flow:
  1. Read iteration_id from payload → load Iteration, Experiment, Template.
  2. Check job status; bail if cancelled.
  3. Load AgentVersion → fetch agent.py from MinIO.
  4. Resolve task_ids (experiment.task_ids → template.default_task_ids).
  5. Set iteration.started_at.
  6. Run run_benchmark().
  7. Insert Trial rows; upload traces + verifier output to MinIO.
  8. Update iteration score/status.
  9. Update experiment best pointer if improved.
  10. Enqueue optimizer.propose.
  11. All in one transaction.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from core.benchmark_runner import BenchmarkResult, TrialResult, run_benchmark
from core.config import get_settings
from core.models import AgentVersion, Experiment, Iteration, Job, Template, Trial
from core import queue as q
from core import storage

settings = get_settings()

log = structlog.get_logger(__name__)


async def handle_benchmark_run(session: AsyncSession, payload: dict) -> None:
    """
    Handle a benchmark.run queue task.

    Args:
        session:  Async DB session (not yet committed; caller commits on return).
        payload:  Queue task payload with 'iteration_id'.
    """
    iteration_id: str = payload["iteration_id"]

    log.info("benchmark_run.start", iteration_id=iteration_id)

    # ── 1. Load rows ───────────────────────────────────────────────────────────
    iteration = await session.get(Iteration, iteration_id)
    if iteration is None:
        log.error("benchmark_run.iteration_not_found", iteration_id=iteration_id)
        raise ValueError(f"Iteration {iteration_id} not found")

    experiment = await session.get(Experiment, iteration.experiment_id)
    if experiment is None:
        raise ValueError(f"Experiment {iteration.experiment_id} not found")

    job = await session.get(Job, iteration.job_id)
    if job is None:
        raise ValueError(f"Job {iteration.job_id} not found")

    template = await session.get(Template, experiment.template_id)
    if template is None:
        raise ValueError(f"Template {experiment.template_id} not found")

    # ── 2. Cancellation check ─────────────────────────────────────────────────
    if job.status == "cancelled":
        log.info("benchmark_run.cancelled", iteration_id=iteration_id, job_id=job.id)
        iteration.status = "failed"
        iteration.error_message = "Job was cancelled before benchmark started."
        iteration.finished_at = datetime.now(timezone.utc)
        session.add(iteration)
        return  # caller commits; no further enqueue

    # ── 3. Load agent .py ─────────────────────────────────────────────────────
    agent_version = await session.get(AgentVersion, iteration.agent_version_id)
    if agent_version is None:
        raise ValueError(f"AgentVersion {iteration.agent_version_id} not found")

    try:
        agent_py_text = storage.get_object_text(agent_version.source_uri)
    except Exception as exc:
        await _fail_iteration(session, iteration, job, f"Failed to fetch agent.py: {exc}")
        raise

    # ── 4. Resolve task_ids ───────────────────────────────────────────────────
    task_ids: list[str] | None = experiment.task_ids or template.default_task_ids
    if not task_ids:
        await _fail_iteration(
            session, iteration, job,
            "No task_ids on experiment or template. Cannot run benchmark."
        )
        raise ValueError("task_ids is empty")

    # ── 5. Set started_at ─────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    if iteration.started_at is None:
        iteration.started_at = now
        session.add(iteration)

    # ── 6. Run benchmark ──────────────────────────────────────────────────────
    try:
        bench_result: BenchmarkResult = await run_benchmark(
            agent_py_text=agent_py_text,
            task_ids=task_ids,
            model=settings.benchmark_model,  # AGENT_MODEL in sandbox env (per design §5.15: inner agent uses BENCHMARK_MODEL, not the optimizer model)
            sandbox_provider=experiment.sandbox_provider,
            n_concurrent=experiment.max_concurrency,
            per_task_timeout_seconds=1200,
        )
    except Exception as exc:
        log.exception("benchmark_run.run_failed", iteration_id=iteration_id, exc=str(exc))
        await _fail_iteration(session, iteration, job, f"run_benchmark raised: {exc}")
        raise

    finished_at = datetime.now(timezone.utc)

    # ── 7. Insert Trial rows + upload artefacts ───────────────────────────────
    exp_id = experiment.id
    iter_n = iteration.iteration_number

    for trial_result in bench_result.trials:
        status = "done" if trial_result.infra_error is None else "infra_error"
        trial = Trial(
            iteration_id=iteration_id,
            task_id=trial_result.task_id,
            status=status,
            reward=Decimal(str(trial_result.reward)) if trial_result.reward is not None else None,
            wall_time_ms=trial_result.wall_time_ms,
            command_count=trial_result.command_count,
            sandbox_id=trial_result.sandbox_id,
            infra_error=trial_result.infra_error,
            finished_at=finished_at,
        )

        # Upload trace.
        if trial_result.trace is not None:
            trace_uri = (
                f"experiments/{exp_id}/iterations/{iter_n}/"
                f"trials/{trial_result.task_id}/trace.json"
            )
            try:
                storage.put_object(
                    trace_uri,
                    json.dumps(trial_result.trace, indent=2, default=str),
                    content_type="application/json",
                )
                trial.trace_uri = trace_uri
            except Exception as exc:
                log.warning(
                    "benchmark_run.trace_upload_failed",
                    task_id=trial_result.task_id,
                    exc=str(exc),
                )

        # Upload verifier output.
        if trial_result.verifier_output is not None:
            verifier_uri = (
                f"experiments/{exp_id}/iterations/{iter_n}/"
                f"trials/{trial_result.task_id}/verifier.txt"
            )
            try:
                storage.put_object(
                    verifier_uri,
                    trial_result.verifier_output,
                    content_type="text/plain",
                )
                trial.verifier_output_uri = verifier_uri
            except Exception as exc:
                log.warning(
                    "benchmark_run.verifier_upload_failed",
                    task_id=trial_result.task_id,
                    exc=str(exc),
                )

        session.add(trial)

    # ── 8. Update iteration ───────────────────────────────────────────────────
    score = Decimal(str(bench_result.score))
    iteration.score = score
    iteration.status = "done"
    iteration.finished_at = finished_at

    # ── 9. Decide outcome + update experiment ─────────────────────────────────
    is_baseline = iteration.iteration_number == 0

    if is_baseline:
        iteration.outcome = "baseline"
        iteration.best_pointer_changed = True
        experiment.best_agent_version_id = iteration.agent_version_id
        experiment.best_score = score
        experiment.status = "ready"
        experiment.updated_at = finished_at
    else:
        current_best = experiment.best_score
        if current_best is None or score > current_best:
            iteration.outcome = "kept"
            iteration.best_pointer_changed = True
            experiment.best_agent_version_id = iteration.agent_version_id
            experiment.best_score = score
            experiment.updated_at = finished_at
        else:
            iteration.outcome = "reverted"
            iteration.best_pointer_changed = False

    session.add(iteration)
    session.add(experiment)

    # ── 10. Enqueue optimizer.propose ─────────────────────────────────────────
    await q.enqueue(
        session,
        task_type="optimizer.propose",
        payload={"iteration_id": iteration_id},
    )

    log.info(
        "benchmark_run.done",
        iteration_id=iteration_id,
        iteration_number=iter_n,
        score=float(score),
        outcome=iteration.outcome,
        n_trials=len(bench_result.trials),
    )


async def _fail_iteration(
    session: AsyncSession,
    iteration: Iteration,
    job: Job,
    error_message: str,
) -> None:
    """Mark the iteration and job as failed. Does NOT enqueue further."""
    now = datetime.now(timezone.utc)
    iteration.status = "failed"
    iteration.error_message = error_message
    iteration.finished_at = now
    session.add(iteration)

    job.status = "failed"
    job.stop_reason = "error"
    job.error_message = error_message
    job.finished_at = now
    session.add(job)

    log.error(
        "benchmark_run.iteration_failed",
        iteration_id=iteration.id,
        job_id=job.id,
        error=error_message,
    )
