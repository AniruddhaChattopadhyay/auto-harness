"""
core/queue.py — Postgres-backed task queue operations.

All functions are async and take an AsyncSession managed by the caller.
The SELECT … FOR UPDATE SKIP LOCKED claim is done via raw SQL (text()) because
SQLModel/SQLAlchemy ORM does not compose SKIP LOCKED cleanly across all
backends, and raw SQL is the canonical pattern for this use case.

Queue flow:
  enqueue()      → INSERT pending row, returns id
  claim_one()    → SELECT FOR UPDATE SKIP LOCKED → set running, return row
  mark_done()    → set done + finished_at
  mark_failed()  → if attempts >= max_attempts → dead_letter; else re-pend w/ backoff
  sweep_stale()  → find running rows past timeout → re-pend or dead_letter
"""

from __future__ import annotations

import datetime
import json
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from core.models import TaskQueue


async def enqueue(
    session: AsyncSession,
    task_type: str,
    payload: dict,
    scheduled_for: Optional[datetime.datetime] = None,
) -> str:
    """
    Insert a new task_queue row and return its id.

    Must be called inside a transaction managed by the caller (the session
    will be committed / rolled back by the caller's context manager).
    """
    task = TaskQueue(
        task_type=task_type,
        payload=payload,
        status="pending",
        attempts=0,
        scheduled_for=scheduled_for or datetime.datetime.now(datetime.timezone.utc),
    )
    session.add(task)
    await session.flush()  # assign id without committing yet
    return task.id


async def claim_one(
    session: AsyncSession,
    task_type: Optional[str] = None,
) -> Optional[TaskQueue]:
    """
    Claim the next claimable pending task using SELECT … FOR UPDATE SKIP LOCKED.

    Args:
        session:   Async DB session.
        task_type: Optional filter — if provided, only claim tasks of this type
                   (e.g. 'benchmark.run' or 'optimizer.propose').

    Returns the TaskQueue row (already mutated to status='running') or None if
    the queue has no eligible rows.
    """
    if task_type is not None:
        sql = text(
            """
            SELECT id
            FROM   task_queue
            WHERE  status      = 'pending'
              AND  scheduled_for <= now()
              AND  task_type   = :task_type
            ORDER  BY scheduled_for, created_at
            FOR    UPDATE SKIP LOCKED
            LIMIT  1
            """
        )
        result = await session.execute(sql, {"task_type": task_type})
    else:
        sql = text(
            """
            SELECT id
            FROM   task_queue
            WHERE  status = 'pending'
              AND  scheduled_for <= now()
            ORDER  BY scheduled_for, created_at
            FOR    UPDATE SKIP LOCKED
            LIMIT  1
            """
        )
        result = await session.execute(sql)
    row = result.fetchone()
    if row is None:
        return None

    task_id: str = row[0]

    now = datetime.datetime.now(datetime.timezone.utc)
    update_sql = text(
        """
        UPDATE task_queue
        SET    status     = 'running',
               claimed_at = :now,
               attempts   = attempts + 1
        WHERE  id = :id
        """
    )
    await session.execute(update_sql, {"now": now, "id": task_id})

    # Reload the row so the caller gets the mutated object.
    get_sql = text("SELECT * FROM task_queue WHERE id = :id")
    result2 = await session.execute(get_sql, {"id": task_id})
    raw = result2.mappings().one()

    # Reconstruct a TaskQueue model from the raw mapping.
    task = TaskQueue.model_validate(dict(raw))
    return task


async def mark_done(session: AsyncSession, task_id: str) -> None:
    """Mark a running task as done."""
    now = datetime.datetime.now(datetime.timezone.utc)
    await session.execute(
        text(
            """
            UPDATE task_queue
            SET    status      = 'done',
                   finished_at = :now
            WHERE  id = :id
            """
        ),
        {"now": now, "id": task_id},
    )


async def mark_failed(
    session: AsyncSession,
    task_id: str,
    error: str,
    retry: bool = True,
) -> None:
    """
    Mark a task as failed.

    If ``retry`` is False or ``attempts >= max_attempts``, the task goes to
    ``dead_letter``.  Otherwise it is re-queued as ``pending`` with an
    exponential backoff (30 s × attempts).
    """
    now = datetime.datetime.now(datetime.timezone.utc)

    # Fetch current attempts + max_attempts.
    row = (
        await session.execute(
            text("SELECT attempts, max_attempts FROM task_queue WHERE id = :id"),
            {"id": task_id},
        )
    ).fetchone()

    if row is None:
        return  # Task vanished — nothing to do.

    attempts, max_attempts = row[0], row[1]

    if not retry or attempts >= max_attempts:
        await session.execute(
            text(
                """
                UPDATE task_queue
                SET    status      = 'dead_letter',
                       last_error  = :error,
                       finished_at = :now
                WHERE  id = :id
                """
            ),
            {"error": error, "now": now, "id": task_id},
        )
    else:
        backoff_seconds = 30 * attempts
        scheduled_for = now + datetime.timedelta(seconds=backoff_seconds)
        await session.execute(
            text(
                """
                UPDATE task_queue
                SET    status        = 'pending',
                       last_error    = :error,
                       scheduled_for = :scheduled_for,
                       claimed_at    = NULL,
                       claimed_by    = NULL
                WHERE  id = :id
                """
            ),
            {"error": error, "scheduled_for": scheduled_for, "id": task_id},
        )


async def sweep_stale(session: AsyncSession, timeout_seconds: int) -> int:
    """
    Find ``running`` rows whose ``claimed_at`` is older than *timeout_seconds*
    and re-queue them (or move to ``dead_letter`` if max_attempts exhausted).

    Returns the count of rows swept.
    """
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=timeout_seconds
    )

    # Find stale running rows.
    stale_sql = text(
        """
        SELECT id, attempts, max_attempts
        FROM   task_queue
        WHERE  status     = 'running'
          AND  claimed_at < :cutoff
        FOR    UPDATE SKIP LOCKED
        """
    )
    result = await session.execute(stale_sql, {"cutoff": cutoff})
    rows = result.fetchall()

    if not rows:
        return 0

    now = datetime.datetime.now(datetime.timezone.utc)
    swept = 0
    for (task_id, attempts, max_attempts) in rows:
        if attempts >= max_attempts:
            await session.execute(
                text(
                    """
                    UPDATE task_queue
                    SET    status      = 'dead_letter',
                           last_error  = 'stale: timed out',
                           finished_at = :now
                    WHERE  id = :id
                    """
                ),
                {"now": now, "id": task_id},
            )
        else:
            backoff_seconds = 30 * attempts
            scheduled_for = now + datetime.timedelta(seconds=backoff_seconds)
            await session.execute(
                text(
                    """
                    UPDATE task_queue
                    SET    status        = 'pending',
                           last_error    = 'stale: timed out',
                           scheduled_for = :scheduled_for,
                           claimed_at    = NULL,
                           claimed_by    = NULL
                    WHERE  id = :id
                    """
                ),
                {"scheduled_for": scheduled_for, "id": task_id},
            )
        swept += 1

    return swept
