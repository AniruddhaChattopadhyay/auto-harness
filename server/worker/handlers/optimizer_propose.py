"""
worker/handlers/optimizer_propose.py — Handler for optimizer.propose queue tasks.

See design.md §5.11, §5.12, §5.14.

Flow:
  1. Load iteration_id from payload.
  2. Check job.status; bail if cancelled.
  3. Load all iterations of the experiment for stop-condition checks.
  4. Check stop conditions (max_iterations, perfect_score, stagnation, wall_time).
  5. If stopping: finalise job, return without enqueueing.
  6. Otherwise:
     a. Build scratch_dir in /tmp/optimizer/{iteration_id}/.
     b. Pull current best agent.py and rolling learnings.md from MinIO.
     c. Pull traces + verifier output for the just-finished iteration.
     d. Write INSTRUCTIONS.md from template.
     e. Run outer agent.
  7. Upload result artefacts to MinIO.
  8. Insert AgentVersion + Iteration rows.
  9. Enqueue benchmark.run for the new iteration.
  10. Cleanup scratch_dir.
  All database changes in a single transaction.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import structlog
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from core import queue as q
from core import storage
from core.models import AgentVersion, Experiment, Iteration, Job, Template, Trial
from worker.outer_agent.registry import make_outer_agent

log = structlog.get_logger(__name__)

# Default optimizer timeout (seconds).
_DEFAULT_OPTIMIZER_TIMEOUT = 600  # 10 minutes


async def handle_optimizer_propose(session: AsyncSession, payload: dict) -> None:
    """
    Handle an optimizer.propose queue task.

    Args:
        session:  Async DB session (not yet committed; caller commits on return).
        payload:  Queue task payload with 'iteration_id'.
    """
    iteration_id: str = payload["iteration_id"]

    log.info("optimizer_propose.start", iteration_id=iteration_id)

    # ── 1. Load source iteration ───────────────────────────────────────────────
    iteration = await session.get(Iteration, iteration_id)
    if iteration is None:
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
        log.info("optimizer_propose.cancelled", iteration_id=iteration_id, job_id=job.id)
        job.stop_reason = "cancelled"
        job.finished_at = datetime.now(timezone.utc)
        session.add(job)
        return

    # ── 3. Load all iterations for this experiment ────────────────────────────
    result = await session.execute(
        select(Iteration)
        .where(Iteration.experiment_id == experiment.id)
        .order_by(Iteration.iteration_number)
    )
    all_iterations: list[Iteration] = list(result.scalars().all())

    # ── 4. Check stop conditions ──────────────────────────────────────────────
    stopping_criteria = job.stopping_criteria or {}

    stop_reason = _check_stop_conditions(
        all_iterations=all_iterations,
        job=job,
        experiment=experiment,
        stopping_criteria=stopping_criteria,
    )

    if stop_reason is not None:
        log.info(
            "optimizer_propose.stopping",
            iteration_id=iteration_id,
            stop_reason=stop_reason,
        )
        job.status = "done"
        job.stop_reason = stop_reason
        job.finished_at = datetime.now(timezone.utc)
        job.ended_at_iteration = iteration.iteration_number
        session.add(job)
        return

    # ── 5. Build scratch dir ──────────────────────────────────────────────────
    scratch_dir = Path(f"/tmp/optimizer/{iteration_id}")
    scratch_dir.mkdir(parents=True, exist_ok=True)

    try:
        await _run_optimizer(
            session=session,
            scratch_dir=scratch_dir,
            iteration=iteration,
            experiment=experiment,
            job=job,
            template=template,
            all_iterations=all_iterations,
        )
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)


async def _run_optimizer(
    session: AsyncSession,
    scratch_dir: Path,
    iteration: Iteration,
    experiment: Experiment,
    job: Job,
    template: Template,
    all_iterations: list[Iteration],
) -> None:
    """Populate scratch_dir, run outer agent, persist results, enqueue next."""

    exp_id = experiment.id
    next_iter_number = iteration.iteration_number + 1

    # ── a. Pull current best agent.py ────────────────────────────────────────
    best_av = await session.get(AgentVersion, experiment.best_agent_version_id)
    if best_av is None:
        raise ValueError("experiment.best_agent_version_id is NULL — cannot optimise")

    try:
        agent_py_text = storage.get_object_text(best_av.source_uri)
    except Exception as exc:
        raise ValueError(f"Failed to fetch best agent.py from MinIO: {exc}") from exc

    (scratch_dir / "agent.py").write_text(agent_py_text, encoding="utf-8")

    # ── b. Pull rolling learnings.md ─────────────────────────────────────────
    learnings_uri = f"experiments/{exp_id}/learnings.md"
    learnings_text = ""
    if storage.object_exists(learnings_uri):
        try:
            learnings_text = storage.get_object_text(learnings_uri)
        except Exception as exc:
            log.warning("optimizer_propose.learnings_load_failed", exc=str(exc))

    (scratch_dir / "learnings.md").write_text(learnings_text, encoding="utf-8")

    # ── c. Pull traces for the just-finished iteration ────────────────────────
    # Train/test holdout (design §5.10 anti-cheating): traces for tasks listed in
    # experiment.test_task_ids are NEVER materialized in the scratch dir. The
    # optimizer cannot see them, so it cannot overfit to held-out tasks.
    traces_dir = scratch_dir / "traces"
    iter_n = iteration.iteration_number
    test_task_ids = set(experiment.test_task_ids or [])

    result = await session.execute(
        select(Trial).where(Trial.iteration_id == iteration.id)
    )
    trials: list[Trial] = list(result.scalars().all())

    skipped_test = []
    for trial in trials:
        if trial.task_id in test_task_ids:
            skipped_test.append(trial.task_id)
            continue  # held-out test task — do NOT expose to optimizer

        task_traces_dir = traces_dir / trial.task_id
        task_traces_dir.mkdir(parents=True, exist_ok=True)

        if trial.trace_uri:
            try:
                trace_bytes = storage.get_object(trial.trace_uri)
                (task_traces_dir / "trace.json").write_bytes(trace_bytes)
            except Exception as exc:
                log.warning(
                    "optimizer_propose.trace_load_failed",
                    task_id=trial.task_id,
                    exc=str(exc),
                )

        if trial.verifier_output_uri:
            try:
                v_bytes = storage.get_object(trial.verifier_output_uri)
                (task_traces_dir / "verifier.txt").write_bytes(v_bytes)
            except Exception as exc:
                log.warning(
                    "optimizer_propose.verifier_load_failed",
                    task_id=trial.task_id,
                    exc=str(exc),
                )

    if skipped_test:
        log.info(
            "optimizer_propose.test_traces_held_out",
            iteration_id=iteration.id,
            test_task_ids=skipped_test,
        )

    # ── d. Write INSTRUCTIONS.md ──────────────────────────────────────────────
    instructions = template.instruction_template
    (scratch_dir / "INSTRUCTIONS.md").write_text(instructions, encoding="utf-8")

    # Upload context inputs to MinIO for audit trail.
    _try_upload(
        f"experiments/{exp_id}/iterations/{next_iter_number}/optimizer/context/agent_in.py",
        agent_py_text,
    )
    _try_upload(
        f"experiments/{exp_id}/iterations/{next_iter_number}/optimizer/context/learnings_in.md",
        learnings_text,
    )
    instructions_uri = (
        f"experiments/{exp_id}/iterations/{next_iter_number}/optimizer/instructions.md"
    )
    _try_upload(instructions_uri, instructions)

    # ── e. Run outer agent ────────────────────────────────────────────────────
    outer_agent = make_outer_agent(experiment.optimizer_kind)
    optimizer_timeout = int(
        job.stopping_criteria.get("optimizer_timeout_seconds", _DEFAULT_OPTIMIZER_TIMEOUT)
    )

    log.info(
        "optimizer_propose.running_outer_agent",
        iteration_id=iteration.id,
        optimizer_kind=experiment.optimizer_kind,
        model=experiment.optimizer_model,
        timeout_s=optimizer_timeout,
    )

    result_obj = await outer_agent.run(
        scratch_dir=scratch_dir,
        instructions=instructions,
        model=experiment.optimizer_model,
        timeout_seconds=optimizer_timeout,
    )

    # ── 7. Upload artefacts ────────────────────────────────────────────────────
    transcript_uri = (
        f"experiments/{exp_id}/iterations/{next_iter_number}/optimizer/transcript.json"
    )
    _try_upload(
        transcript_uri,
        json.dumps(result_obj.transcript, indent=2, default=str),
        content_type="application/json",
    )

    new_agent_uri = f"experiments/{exp_id}/agents/v{next_iter_number}.py"
    _try_upload(new_agent_uri, result_obj.new_agent_py)
    # Also store as optimizer/agent_out.py for the audit trail.
    _try_upload(
        f"experiments/{exp_id}/iterations/{next_iter_number}/optimizer/agent_out.py",
        result_obj.new_agent_py,
    )

    # Overwrite rolling learnings.md.
    _try_upload(learnings_uri, result_obj.new_learnings_md)
    _try_upload(
        f"experiments/{exp_id}/iterations/{next_iter_number}/optimizer/learnings_out.md",
        result_obj.new_learnings_md,
    )

    # ── Handle outer-agent failure ────────────────────────────────────────────
    if result_obj.status != "ok":
        log.error(
            "optimizer_propose.outer_agent_failed",
            status=result_obj.status,
            error=result_obj.error_message,
        )
        # Insert a failed iteration + fail the job.
        new_av = AgentVersion(
            experiment_id=exp_id,
            version_number=next_iter_number,
            parent_id=best_av.id,
            source_uri=new_agent_uri,
            source_hash=_sha256(result_obj.new_agent_py),
        )
        session.add(new_av)
        await session.flush()

        new_iter = Iteration(
            experiment_id=exp_id,
            job_id=job.id,
            iteration_number=next_iter_number,
            parent_iteration_id=iteration.id,
            agent_version_id=new_av.id,
            parent_agent_version_id=best_av.id,
            status="failed",
            optimizer_diagnosis=result_obj.diagnosis,
            optimizer_instructions_uri=instructions_uri,
            optimizer_transcript_uri=transcript_uri,
            optimizer_input_tokens=result_obj.input_tokens,
            optimizer_output_tokens=result_obj.output_tokens,
            optimizer_cost_usd=(
                Decimal(str(result_obj.cost_usd)) if result_obj.cost_usd is not None else None
            ),
            error_message=result_obj.error_message or result_obj.status,
            finished_at=datetime.now(timezone.utc),
        )
        session.add(new_iter)

        job.status = "failed"
        job.stop_reason = "error"
        job.error_message = result_obj.error_message or "outer agent returned non-ok status"
        job.finished_at = datetime.now(timezone.utc)
        session.add(job)
        return

    # ── 8. Insert AgentVersion + Iteration ────────────────────────────────────
    new_agent_py_hash = _sha256(result_obj.new_agent_py)

    new_av = AgentVersion(
        experiment_id=exp_id,
        version_number=next_iter_number,
        parent_id=best_av.id,
        source_uri=new_agent_uri,
        source_hash=new_agent_py_hash,
    )
    session.add(new_av)
    await session.flush()  # generate new_av.id

    new_iter = Iteration(
        experiment_id=exp_id,
        job_id=job.id,
        iteration_number=next_iter_number,
        parent_iteration_id=iteration.id,
        agent_version_id=new_av.id,
        parent_agent_version_id=best_av.id,
        status="running",
        optimizer_diagnosis=result_obj.diagnosis,
        expected_targets=result_obj.expected_targets or None,
        optimizer_instructions_uri=instructions_uri,
        optimizer_transcript_uri=transcript_uri,
        optimizer_input_tokens=result_obj.input_tokens,
        optimizer_output_tokens=result_obj.output_tokens,
        optimizer_cost_usd=(
            Decimal(str(result_obj.cost_usd)) if result_obj.cost_usd is not None else None
        ),
    )
    session.add(new_iter)
    await session.flush()  # generate new_iter.id

    # ── 9. Enqueue next benchmark.run ─────────────────────────────────────────
    await q.enqueue(
        session,
        task_type="benchmark.run",
        payload={"iteration_id": new_iter.id},
    )

    log.info(
        "optimizer_propose.done",
        source_iteration=iteration.id,
        new_iteration_id=new_iter.id,
        new_iteration_number=next_iter_number,
        cost_usd=result_obj.cost_usd,
    )


def _check_stop_conditions(
    all_iterations: list[Iteration],
    job: Job,
    experiment: Experiment,
    stopping_criteria: dict,
) -> str | None:
    """
    Evaluate all stop conditions in priority order.

    Returns the stop_reason string or None to continue.

    Stop conditions (in order):
      1. max_iterations: total done iterations ≥ job.max_iterations + 1
         (the +1 accounts for the baseline)
      2. perfect_score: best_score >= 1.0 AND stopping_criteria.perfect_score_stop
      3. stagnation: last N iterations all outcome='reverted'
      4. wall_time: elapsed wall time > stopping_criteria.wall_time_seconds
    """
    done_iterations = [i for i in all_iterations if i.status == "done"]
    n_done = len(done_iterations)

    # 1. max_iterations
    if n_done >= job.max_iterations + 1:
        return "max_iterations"

    # 2. perfect_score
    perfect_score_stop = stopping_criteria.get("perfect_score_stop", True)
    if perfect_score_stop:
        best = experiment.best_score
        if best is not None and best >= Decimal("1.0"):
            return "perfect_score"

    # 3. stagnation
    stagnation_threshold = int(stopping_criteria.get("stagnation_threshold", 3))
    if stagnation_threshold > 0 and n_done >= stagnation_threshold:
        # The baseline (iteration 0) has outcome='baseline', not 'reverted'.
        # Only look at non-baseline iterations.
        non_baseline = [i for i in done_iterations if i.outcome != "baseline"]
        if len(non_baseline) >= stagnation_threshold:
            last_n = non_baseline[-stagnation_threshold:]
            if all(i.outcome == "reverted" for i in last_n):
                return "stagnation"

    # 4. wall_time
    wall_time_seconds = stopping_criteria.get("wall_time_seconds")
    if wall_time_seconds is not None and job.started_at is not None:
        now = datetime.now(timezone.utc)
        # job.started_at may be naive or aware.
        started = job.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        elapsed = (now - started).total_seconds()
        if elapsed > wall_time_seconds:
            return "wall_time"

    return None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _try_upload(
    uri: str,
    data: str | bytes,
    content_type: str = "application/octet-stream",
) -> None:
    """Upload to MinIO, logging a warning on failure (non-fatal)."""
    try:
        storage.put_object(uri, data, content_type=content_type)
    except Exception as exc:
        log.warning("optimizer_propose.upload_failed", uri=uri, exc=str(exc))
