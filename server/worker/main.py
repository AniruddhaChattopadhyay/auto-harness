"""
worker/main.py — Queue consumer loop entry point.

Runs an asyncio event loop with one consumer coroutine per task type
(``benchmark.run`` and ``optimizer.propose``). Each consumer polls its PGMQ
queue with ``pgmq.read``; the message becomes invisible for
``worker_task_timeout_seconds`` while it's being processed and is
auto-revived by PGMQ if the worker crashes — so no separate stale-claim
sweeper is needed.

Graceful shutdown on SIGTERM: cancel consumer coroutines, finish in-flight
work, then dispose the DB engine.

Usage::

    uv run python -m worker.main
    # or
    uv run python worker/main.py
"""

from __future__ import annotations

import asyncio
import signal

import structlog

from core.config import get_settings
from core.db import dispose_engine, get_session, init_engine
from core import queue as q
from worker.handlers.benchmark_run import handle_benchmark_run
from worker.handlers.optimizer_propose import handle_optimizer_propose

log = structlog.get_logger(__name__)


# ── Dispatcher ────────────────────────────────────────────────────────────────

_HANDLERS = {
    "benchmark.run": handle_benchmark_run,
    "optimizer.propose": handle_optimizer_propose,
}


async def _process_one(task_type: str, visibility_timeout: int) -> bool:
    """
    Claim one task of *task_type*, dispatch it, mark done or failed.

    Returns True if a task was processed, False if the queue was empty.
    """
    async with get_session() as session:
        task = await q.claim_one(
            session,
            task_type=task_type,
            visibility_timeout_seconds=visibility_timeout,
        )
        # Commit the read so PGMQ's vt update is visible to other workers.
        await session.commit()

    if task is None:
        return False

    task_id = task.id
    handler = _HANDLERS[task_type]
    log.info(
        "worker.task_claimed",
        task_id=task_id,
        task_type=task_type,
        attempt=task.attempts,
    )

    try:
        async with get_session() as session:
            await handler(session, task.payload)
            # session auto-commits on clean exit (see core/db.py).

        async with get_session() as session:
            await q.mark_done(session, task_type, task_id)

        log.info("worker.task_done", task_id=task_id, task_type=task_type)

    except Exception as exc:
        log.exception(
            "worker.task_failed",
            task_id=task_id,
            task_type=task_type,
            attempt=task.attempts,
            exc=str(exc),
        )
        try:
            async with get_session() as session:
                await q.mark_failed(
                    session,
                    task_type=task_type,
                    task_id=task_id,
                    error=str(exc),
                    attempts=task.attempts,
                    max_attempts=task.max_attempts,
                    retry=True,
                )
        except Exception:
            log.exception("worker.mark_failed_error", task_id=task_id)

    return True


# ── Consumer loops ────────────────────────────────────────────────────────────


async def _consumer_loop(
    task_type: str,
    concurrency: int,
    poll_interval: float,
    visibility_timeout: int,
) -> None:
    """
    Run *concurrency* parallel workers consuming tasks of *task_type*.

    Each slot polls the queue; if empty it backs off for *poll_interval*
    seconds before retrying.
    """
    log.info(
        "worker.consumer_start",
        task_type=task_type,
        concurrency=concurrency,
        poll_interval=poll_interval,
        visibility_timeout=visibility_timeout,
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def _slot() -> None:
        while True:
            async with semaphore:
                found = await _process_one(task_type, visibility_timeout)
            if not found:
                await asyncio.sleep(poll_interval)

    tasks = [asyncio.create_task(_slot()) for _ in range(concurrency)]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    """Initialise and run the worker process."""
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(),
        ],
        logger_factory=structlog.PrintLoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(20),  # INFO
    )

    settings = get_settings()
    init_engine(settings.database_url)

    log.info(
        "worker.starting",
        benchmark_concurrency=settings.worker_benchmark_concurrency,
        optimizer_concurrency=settings.worker_optimizer_concurrency,
        poll_interval=settings.worker_poll_interval_seconds,
        task_timeout=settings.worker_task_timeout_seconds,
    )

    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def _handle_sigterm() -> None:
        log.info("worker.sigterm_received")
        shutdown_event.set()

    loop.add_signal_handler(signal.SIGTERM, _handle_sigterm)
    loop.add_signal_handler(signal.SIGINT, _handle_sigterm)

    benchmark_task = asyncio.create_task(
        _consumer_loop(
            task_type="benchmark.run",
            concurrency=settings.worker_benchmark_concurrency,
            poll_interval=settings.worker_poll_interval_seconds,
            visibility_timeout=settings.worker_task_timeout_seconds,
        ),
        name="benchmark_consumer",
    )
    optimizer_task = asyncio.create_task(
        _consumer_loop(
            task_type="optimizer.propose",
            concurrency=settings.worker_optimizer_concurrency,
            poll_interval=settings.worker_poll_interval_seconds,
            visibility_timeout=settings.worker_task_timeout_seconds,
        ),
        name="optimizer_consumer",
    )

    await shutdown_event.wait()

    log.info("worker.shutting_down")
    for task in (benchmark_task, optimizer_task):
        task.cancel()

    await asyncio.gather(benchmark_task, optimizer_task, return_exceptions=True)

    await dispose_engine()
    log.info("worker.stopped")


if __name__ == "__main__":
    asyncio.run(main())
