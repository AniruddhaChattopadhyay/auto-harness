"""
core/queue.py — Task queue operations backed by the PGMQ Postgres extension.

PGMQ provides SKIP LOCKED + visibility timeout semantics natively, so this
module is a thin wrapper that maps the application's ``task_type`` strings
to PGMQ queue names and exposes the same enqueue / claim / mark_done /
mark_failed surface the rest of the codebase already uses.

Queue mapping:
  benchmark.run      → pgmq queue "benchmark_run"
  optimizer.propose  → pgmq queue "optimizer_propose"

Why PGMQ instead of a hand-rolled table:
  - Visibility timeout makes a separate stale-claim sweeper unnecessary.
  - read_ct on each message gives us retry counting for free.
  - All operations are SQL functions, so they compose with the caller's
    transaction (enqueue lands atomically with the surrounding work).
"""

from __future__ import annotations

import datetime
import json
from dataclasses import dataclass
from typing import Optional

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger(__name__)


# ── Task type ↔ queue name mapping ────────────────────────────────────────────

_TASK_TYPE_TO_QUEUE: dict[str, str] = {
    "benchmark.run": "benchmark_run",
    "optimizer.propose": "optimizer_propose",
}

DEFAULT_MAX_ATTEMPTS = 3


def _queue_for(task_type: str) -> str:
    try:
        return _TASK_TYPE_TO_QUEUE[task_type]
    except KeyError as exc:
        raise ValueError(f"Unknown task_type: {task_type!r}") from exc


# ── Public dataclass returned by claim_one ────────────────────────────────────


@dataclass(slots=True)
class ClaimedTask:
    """A message claimed from PGMQ, ready to be handed to a handler."""

    id: str           # PGMQ msg_id, stringified
    task_type: str    # e.g. "benchmark.run"
    payload: dict
    attempts: int     # PGMQ read_ct (1 on first read)
    max_attempts: int


# ── Operations ────────────────────────────────────────────────────────────────


async def enqueue(
    session: AsyncSession,
    task_type: str,
    payload: dict,
    scheduled_for: Optional[datetime.datetime] = None,
) -> str:
    """
    Send a message onto the queue corresponding to *task_type*.

    Runs inside the caller's transaction — the send commits/rolls back with
    the rest of the unit of work.

    Returns the PGMQ msg_id as a string.
    """
    queue = _queue_for(task_type)

    delay_seconds = 0
    if scheduled_for is not None:
        now = datetime.datetime.now(datetime.timezone.utc)
        delta = (scheduled_for - now).total_seconds()
        if delta > 0:
            delay_seconds = int(delta)

    result = await session.execute(
        text(
            "SELECT pgmq.send(:q, CAST(:msg AS jsonb), CAST(:delay AS integer))"
        ),
        {"q": queue, "msg": json.dumps(payload), "delay": delay_seconds},
    )
    msg_id = result.scalar_one()
    return str(msg_id)


async def claim_one(
    session: AsyncSession,
    task_type: str,
    visibility_timeout_seconds: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Optional[ClaimedTask]:
    """
    Claim the next available message from the queue for *task_type*.

    Uses PGMQ's ``read``, which atomically returns the next visible message
    and pushes its visibility timeout *visibility_timeout_seconds* into the
    future. If processing crashes the message becomes visible again automatically.

    Returns None if the queue has no claimable messages right now.
    """
    queue = _queue_for(task_type)

    result = await session.execute(
        text(
            "SELECT msg_id, read_ct, message "
            "FROM pgmq.read(:q, CAST(:vt AS integer), 1)"
        ),
        {"q": queue, "vt": visibility_timeout_seconds},
    )
    row = result.fetchone()
    if row is None:
        return None

    msg_id, read_ct, message = row[0], row[1], row[2]

    # PGMQ stores the body as JSONB; asyncpg returns it as a dict already.
    payload = message if isinstance(message, dict) else json.loads(message)

    return ClaimedTask(
        id=str(msg_id),
        task_type=task_type,
        payload=payload,
        attempts=int(read_ct),
        max_attempts=max_attempts,
    )


async def mark_done(
    session: AsyncSession,
    task_type: str,
    task_id: str,
) -> None:
    """Permanently remove a successfully processed message from its queue."""
    queue = _queue_for(task_type)
    await session.execute(
        text("SELECT pgmq.delete(:q, CAST(:id AS bigint))"),
        {"q": queue, "id": int(task_id)},
    )


async def mark_failed(
    session: AsyncSession,
    task_type: str,
    task_id: str,
    error: str,
    attempts: int,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    retry: bool = True,
) -> None:
    """
    Handle a failed message.

    - If ``retry`` and ``attempts < max_attempts``: schedule a backoff by
      pushing the message's visibility timeout out by ``30 * attempts`` seconds.
      Once that window elapses the message becomes claimable again and PGMQ
      will increment read_ct on the next read.
    - Otherwise: archive the message (PGMQ moves it to its dead-letter
      ``pgmq.a_<queue>`` archive table for inspection).

    The *error* is logged by the caller; PGMQ does not store error text on
    the message itself.
    """
    queue = _queue_for(task_type)

    if retry and attempts < max_attempts:
        backoff_seconds = 30 * attempts
        log.warning(
            "queue.task_retry",
            queue=queue, task_id=task_id, attempt=attempts,
            backoff_seconds=backoff_seconds, error=error,
        )
        await session.execute(
            text(
                "SELECT pgmq.set_vt(:q, CAST(:id AS bigint), "
                "CAST(:vt AS integer))"
            ),
            {"q": queue, "id": int(task_id), "vt": backoff_seconds},
        )
    else:
        log.error(
            "queue.task_dead_letter",
            queue=queue, task_id=task_id, attempt=attempts, error=error,
        )
        await session.execute(
            text("SELECT pgmq.archive(:q, CAST(:id AS bigint))"),
            {"q": queue, "id": int(task_id)},
        )
