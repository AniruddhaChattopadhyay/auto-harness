"""
core/benchmark_runner.py — Stateless benchmark ETL via harbor subprocess.

See design.md §10 and the reference TerminalBenchRunner in benchmark.py.

We use the subprocess approach (same as the reference repo) rather than
importing harbor's Python internals, because:
  - The reference repo has proven it works end-to-end.
  - Harbor's Python API surface is not yet stable enough to rely on.
  - Subprocess isolation means harbor crashes can't corrupt our process.

Public entry point
------------------
    result = await run_benchmark(
        agent_py_text=...,
        task_ids=[...],
        model="gpt-5.4",
        sandbox_provider="e2b",
        n_concurrent=5,
        per_task_timeout_seconds=1200,
    )

The caller (worker/handlers/benchmark_run.py) is responsible for writing
TrialResult data to Postgres + MinIO.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# Harbor dataset constant — same as reference repo.
_HARBOR_DATASET = "terminal-bench@2.0"
# Class the inner agent file exports.
_AGENT_IMPORT_PATH = "agent:HarnessAgent"
# Default harbor per-task timeout baseline (seconds) — harbor's internal default.
_HARBOR_BASE_TIMEOUT_S = 180


# ── Result types ───────────────────────────────────────────────────────────────


@dataclass
class TrialResult:
    """
    Result for a single (task_id) run inside a benchmark pass.

    reward=None means an infra error (harbor crashed, verifier didn't run).
    reward=0.0 means the verifier ran and scored it as 0.
    """

    task_id: str
    reward: float | None  # None on infra error; 0.0–1.0 otherwise
    wall_time_ms: int
    command_count: int | None
    sandbox_id: str | None
    trace: list[dict] | None  # full message trace from agent/trace.json
    verifier_output: str | None
    infra_error: str | None


@dataclass
class BenchmarkResult:
    """Aggregate result across all tasks in one benchmark pass."""

    score: float  # mean of non-null rewards; 0.0 if all infra errors
    trials: list[TrialResult]
    wall_time_ms: int


# ── Public function ────────────────────────────────────────────────────────────


async def run_benchmark(
    agent_py_text: str,
    task_ids: list[str],
    model: str,
    sandbox_provider: str,
    n_concurrent: int,
    per_task_timeout_seconds: int = 1200,
) -> BenchmarkResult:
    """
    Run harbor against terminal-bench@2.0 for *task_ids* using *agent_py_text*.

    The agent file is written to a temp dir as ``agent.py``.  Harbor picks it
    up via ``--agent-import-path agent:HarnessAgent``.  The model is injected
    via ``AGENT_MODEL`` env var (the baseline agent reads it with
    ``os.environ.get("AGENT_MODEL", ...)``.

    Returns a BenchmarkResult.  Never raises — infra errors are captured in
    TrialResult.infra_error with reward=None.
    """
    t0 = time.monotonic()

    if not task_ids:
        log.warning("run_benchmark.no_tasks")
        return BenchmarkResult(score=0.0, trials=[], wall_time_ms=0)

    # Write agent.py to a temporary directory that harbor can see.
    tmp_dir = Path(tempfile.mkdtemp(prefix="harness_agent_"))
    jobs_dir = Path(tempfile.mkdtemp(prefix="harness_jobs_"))

    try:
        agent_file = tmp_dir / "agent.py"
        agent_file.write_text(agent_py_text, encoding="utf-8")

        trials = await _run_harbor(
            agent_dir=tmp_dir,
            jobs_dir=jobs_dir,
            task_ids=task_ids,
            model=model,
            sandbox_provider=sandbox_provider,
            n_concurrent=n_concurrent,
            per_task_timeout_seconds=per_task_timeout_seconds,
        )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        shutil.rmtree(jobs_dir, ignore_errors=True)

    wall_ms = int((time.monotonic() - t0) * 1000)

    # Compute mean score over non-null rewards.
    valid_rewards = [t.reward for t in trials if t.reward is not None]
    score = sum(valid_rewards) / len(valid_rewards) if valid_rewards else 0.0

    log.info(
        "run_benchmark.complete",
        n_tasks=len(task_ids),
        n_trials=len(trials),
        n_infra_errors=sum(1 for t in trials if t.infra_error),
        score=score,
        wall_ms=wall_ms,
    )

    return BenchmarkResult(score=score, trials=trials, wall_time_ms=wall_ms)


# ── Internal: harbor subprocess ────────────────────────────────────────────────


async def _run_harbor(
    agent_dir: Path,
    jobs_dir: Path,
    task_ids: list[str],
    model: str,
    sandbox_provider: str,
    n_concurrent: int,
    per_task_timeout_seconds: int,
) -> list[TrialResult]:
    """
    Invoke ``harbor run`` as a subprocess and parse the results directory.

    Returns a list of TrialResult (one per task_id).  Any task not found in
    the output directory gets an infra_error TrialResult with reward=None.
    """
    n = min(n_concurrent, len(task_ids))
    agent_timeout_mult = per_task_timeout_seconds / _HARBOR_BASE_TIMEOUT_S

    cmd = [
        "harbor", "run",
        "-d", _HARBOR_DATASET,
        "--agent-import-path", _AGENT_IMPORT_PATH,
        "--model", model,
        "--env", sandbox_provider,
        "--agent-timeout-multiplier", f"{agent_timeout_mult:.2f}",
        "--jobs-dir", str(jobs_dir),
        "-n", str(n),
        "-y",  # non-interactive
    ]
    for tid in task_ids:
        cmd.extend(["-i", tid])

    env = os.environ.copy()
    # Inject agent dir into PYTHONPATH so harbor can import ``agent:HarnessAgent``.
    env["PYTHONPATH"] = str(agent_dir) + os.pathsep + env.get("PYTHONPATH", "")
    env["AGENT_MODEL"] = model
    # Always save traces — we upload them to MinIO.
    env["HARNESS_SAVE_TRACE"] = "1"

    # Conservative subprocess timeout: per-task * batches + 5 min buffer.
    n_tasks = len(task_ids)
    n_batches = math.ceil(n_tasks / max(n, 1))
    subprocess_timeout_s = per_task_timeout_seconds * n_batches + 300

    log.info(
        "harbor.run",
        n_tasks=n_tasks,
        n_concurrent=n,
        model=model,
        sandbox_provider=sandbox_provider,
        subprocess_timeout_s=subprocess_timeout_s,
    )

    run_start = time.monotonic()

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=subprocess_timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            log.warning("harbor.subprocess_timeout", timeout_s=subprocess_timeout_s)
            # Return all tasks as infra errors.
            wall_ms = int((time.monotonic() - run_start) * 1000)
            return [
                TrialResult(
                    task_id=tid,
                    reward=None,
                    wall_time_ms=wall_ms,
                    command_count=None,
                    sandbox_id=None,
                    trace=None,
                    verifier_output=None,
                    infra_error=f"harbor subprocess timed out after {subprocess_timeout_s}s",
                )
                for tid in task_ids
            ]

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        if stdout:
            log.debug("harbor.stdout", text=stdout[:2000])
        if stderr:
            log.debug("harbor.stderr", text=stderr[:2000])

        if proc.returncode not in (0, None):
            log.warning("harbor.nonzero_exit", rc=proc.returncode, stderr=stderr[:500])

    except FileNotFoundError:
        # harbor CLI not installed.
        log.error("harbor.not_found")
        wall_ms = int((time.monotonic() - run_start) * 1000)
        return [
            TrialResult(
                task_id=tid,
                reward=None,
                wall_time_ms=wall_ms,
                command_count=None,
                sandbox_id=None,
                trace=None,
                verifier_output=None,
                infra_error="harbor CLI not found; is it installed?",
            )
            for tid in task_ids
        ]
    except Exception as exc:
        log.exception("harbor.unexpected_error", exc=str(exc))
        wall_ms = int((time.monotonic() - run_start) * 1000)
        return [
            TrialResult(
                task_id=tid,
                reward=None,
                wall_time_ms=wall_ms,
                command_count=None,
                sandbox_id=None,
                trace=None,
                verifier_output=None,
                infra_error=f"harbor error: {exc}",
            )
            for tid in task_ids
        ]

    # Find the job directory created by this run.  Filter by mtime ≥ run_start.
    all_dirs = [
        d
        for d in jobs_dir.iterdir()
        if d.is_dir() and d.stat().st_mtime >= run_start - 1
    ]
    if not all_dirs:
        log.warning("harbor.no_job_dir", jobs_dir=str(jobs_dir))
        wall_ms = int((time.monotonic() - run_start) * 1000)
        return [
            TrialResult(
                task_id=tid,
                reward=None,
                wall_time_ms=wall_ms,
                command_count=None,
                sandbox_id=None,
                trace=None,
                verifier_output=None,
                infra_error="harbor produced no job output directory",
            )
            for tid in task_ids
        ]

    job_dir = max(all_dirs, key=lambda d: d.stat().st_mtime)
    log.info("harbor.job_dir", path=str(job_dir))

    # Parse per-trial result.json + traces.
    trials = _parse_job_dir(job_dir, task_ids, run_start)
    return trials


def _parse_job_dir(
    job_dir: Path,
    task_ids: list[str],
    run_start: float,
) -> list[TrialResult]:
    """
    Walk job_dir, parse each trial, and return one TrialResult per task_id.

    Trial subdirectories are named ``{task_id}__{timestamp}`` or just
    ``{task_id}``.  We match by stripping a trailing ``__{...}`` suffix.
    """
    # Build map from task_id → trial dir.
    trial_map: dict[str, Path] = {}
    for trial_dir in job_dir.iterdir():
        if not trial_dir.is_dir():
            continue
        # Strip timestamp suffix.
        dir_name = trial_dir.name
        # harbor names dirs like "task-name__20240101T123456"
        base = dir_name.rsplit("__", 1)[0] if "__" in dir_name else dir_name
        trial_map[base] = trial_dir

    wall_ms_default = int((time.monotonic() - run_start) * 1000)
    results: list[TrialResult] = []

    for tid in task_ids:
        trial_dir = trial_map.get(tid)
        if trial_dir is None:
            results.append(
                TrialResult(
                    task_id=tid,
                    reward=None,
                    wall_time_ms=wall_ms_default,
                    command_count=None,
                    sandbox_id=None,
                    trace=None,
                    verifier_output=None,
                    infra_error="trial directory not found in harbor output",
                )
            )
            continue

        results.append(_parse_trial_dir(tid, trial_dir, wall_ms_default))

    return results


def _parse_trial_dir(
    task_id: str,
    trial_dir: Path,
    default_wall_ms: int,
) -> TrialResult:
    """Parse a single trial directory into a TrialResult."""
    result_file = trial_dir / "result.json"
    trace_file = trial_dir / "agent" / "trace.json"
    # Harbor also puts verifier output in result.json.verifier_result.output

    reward: float | None = None
    verifier_output: str | None = None
    wall_time_ms: int = default_wall_ms
    command_count: int | None = None
    sandbox_id: str | None = None
    infra_error: str | None = None

    if not result_file.exists():
        return TrialResult(
            task_id=task_id,
            reward=None,
            wall_time_ms=default_wall_ms,
            command_count=None,
            sandbox_id=None,
            trace=None,
            verifier_output=None,
            infra_error="result.json not found",
        )

    try:
        data = json.loads(result_file.read_bytes())

        # Extract timing.
        duration = data.get("duration_s") or data.get("duration_seconds")
        if duration is not None:
            try:
                wall_time_ms = int(float(duration) * 1000)
            except (ValueError, TypeError):
                pass

        # Extract sandbox_id.
        sandbox_id = data.get("sandbox_id") or data.get("environment_id")

        # Extract reward.
        vr = data.get("verifier_result")
        if vr and isinstance(vr, dict):
            rewards = vr.get("rewards", {})
            if isinstance(rewards, dict):
                raw_reward = rewards.get("reward")
                if raw_reward is not None:
                    reward = float(raw_reward)
                # Else: verifier ran but no reward field → treat as infra error.
            elif isinstance(rewards, (int, float)):
                reward = float(rewards)
            # Extract verifier output text.
            verifier_output = vr.get("output") or vr.get("message") or json.dumps(vr)
        else:
            # verifier_result absent means verifier didn't run (infra error).
            infra_error = "verifier did not run (no verifier_result in result.json)"

        # Extract command count from agent context if available.
        agent_ctx = data.get("agent_context") or {}
        command_count = agent_ctx.get("n_steps") or agent_ctx.get("command_count")

    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        log.warning("parse_trial.result_error", task_id=task_id, exc=str(exc))
        return TrialResult(
            task_id=task_id,
            reward=None,
            wall_time_ms=default_wall_ms,
            command_count=None,
            sandbox_id=None,
            trace=None,
            verifier_output=None,
            infra_error=f"Failed to parse result.json: {exc}",
        )

    # Load trace.
    trace: list[dict] | None = None
    if trace_file.exists():
        try:
            trace = json.loads(trace_file.read_bytes())
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("parse_trial.trace_error", task_id=task_id, exc=str(exc))
            # Non-fatal — still record the reward.

    return TrialResult(
        task_id=task_id,
        reward=reward,
        wall_time_ms=wall_time_ms,
        command_count=command_count,
        sandbox_id=str(sandbox_id) if sandbox_id else None,
        trace=trace,
        verifier_output=verifier_output,
        infra_error=infra_error,
    )
