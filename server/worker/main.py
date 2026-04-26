"""
worker/main.py — Queue consumer loop entry point.

Runs an asyncio event loop with:
  - One consumer coroutine for 'benchmark.run' tasks (concurrency limited).
  - One consumer coroutine for 'optimizer.propose' tasks (concurrency limited).
  - One sweeper coroutine that re-queues stale running tasks every 60 seconds.

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
import sys

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


async def _process_one(task_type: str) -> bool:
    """
    Claim one task of *task_type*, dispatch it, mark done or failed.

    Returns True if a task was processed, False if the queue was empty.
    """
    async with get_session() as session:
        task = await q.claim_one(session, task_type=task_type)
        if task is None:
            return False
        # Commit the claim immediately so the row is visible as 'running'.
        await session.commit()

    task_id = task.id
    handler = _HANDLERS[task_type]
    log.info("worker.task_claimed", task_id=task_id, task_type=task_type)

    try:
        async with get_session() as session:
            await handler(session, task.payload)
            # session auto-commits on clean exit (see core/db.py).

        async with get_session() as session:
            await q.mark_done(session, task_id)

        log.info("worker.task_done", task_id=task_id, task_type=task_type)

    except Exception as exc:
        log.exception(
            "worker.task_failed",
            task_id=task_id,
            task_type=task_type,
            exc=str(exc),
        )
        try:
            async with get_session() as session:
                await q.mark_failed(session, task_id, error=str(exc), retry=True)
        except Exception:
            log.exception("worker.mark_failed_error", task_id=task_id)

    return True


# ── Consumer loops ────────────────────────────────────────────────────────────


async def _consumer_loop(task_type: str, concurrency: int, poll_interval: float) -> None:
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
    )
    semaphore = asyncio.Semaphore(concurrency)

    async def _slot() -> None:
        while True:
            async with semaphore:
                found = await _process_one(task_type)
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


# ── Sweeper ───────────────────────────────────────────────────────────────────


async def _sweeper_loop(timeout_seconds: int, sweep_interval: float = 60.0) -> None:
    """
    Periodically re-queue stale 'running' tasks whose worker has crashed.

    Runs every *sweep_interval* seconds.  Stale = claimed_at older than
    *timeout_seconds*.
    """
    log.info(
        "worker.sweeper_start",
        timeout_seconds=timeout_seconds,
        sweep_interval=sweep_interval,
    )
    while True:
        await asyncio.sleep(sweep_interval)
        try:
            async with get_session() as session:
                swept = await q.sweep_stale(session, timeout_seconds)
            if swept:
                log.info("worker.sweep_done", swept=swept)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("worker.sweep_error")


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    """Initialise and run the worker process."""
    import structlog

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
        ),
        name="benchmark_consumer",
    )
    optimizer_task = asyncio.create_task(
        _consumer_loop(
            task_type="optimizer.propose",
            concurrency=settings.worker_optimizer_concurrency,
            poll_interval=settings.worker_poll_interval_seconds,
        ),
        name="optimizer_consumer",
    )
    sweeper_task = asyncio.create_task(
        _sweeper_loop(
            timeout_seconds=settings.worker_task_timeout_seconds,
        ),
        name="sweeper",
    )

    # Wait until SIGTERM/SIGINT.
    await shutdown_event.wait()

    log.info("worker.shutting_down")
    for task in (benchmark_task, optimizer_task, sweeper_task):
        task.cancel()

    await asyncio.gather(benchmark_task, optimizer_task, sweeper_task, return_exceptions=True)

    await dispose_engine()
    log.info("worker.stopped")


if __name__ == "__main__":
    asyncio.run(main())
