#!/usr/bin/env python3
"""
test_client.py — End-to-end smoke client for the Agent Optimization Service.

Usage:
    uv run python test_client.py [options]

Required: server must be running (default: http://localhost:8000).

Example (no-wait, just verify wiring):
    uv run python test_client.py --no-wait --max-iterations 1 --name smoke-test

Example (full run, waits for completion):
    uv run python test_client.py --max-iterations 3 --template-id terminal_bench
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import httpx


# ── Formatting helpers ─────────────────────────────────────────────────────────

def _fmt_duration(started: Optional[str], finished: Optional[str]) -> str:
    """Return 'Xs' or 'Xm Ys' from ISO-8601 strings, or '-' if not available."""
    if not started:
        return "-"
    try:
        s = datetime.fromisoformat(started.replace("Z", "+00:00"))
        if finished:
            f = datetime.fromisoformat(finished.replace("Z", "+00:00"))
            secs = int((f - s).total_seconds())
        else:
            secs = int((datetime.now(timezone.utc) - s).total_seconds())
        if secs >= 60:
            return f"{secs // 60}m {secs % 60}s"
        return f"{secs}s"
    except Exception:
        return "-"


def _elapsed(start_ts: float) -> str:
    secs = int(time.time() - start_ts)
    if secs >= 60:
        return f"{secs // 60}m {secs % 60}s"
    return f"{secs}s"


def _fmt_score(score: Any) -> str:
    if score is None:
        return "-"
    try:
        return f"{float(score):.4f}"
    except Exception:
        return str(score)


def _fmt_reward(reward: Any) -> str:
    if reward is None:
        return "-"
    try:
        return f"{float(reward):.2f}"
    except Exception:
        return str(reward)


def _fmt_cost(cost: Any) -> str:
    if cost is None:
        return "-"
    try:
        return f"${float(cost):.4f}"
    except Exception:
        return str(cost)


def _check_response(resp: httpx.Response, label: str) -> dict:
    """Raise with a helpful message on 4xx/5xx; return parsed JSON on success."""
    if resp.is_error:
        try:
            detail = resp.json().get("detail", resp.text)
        except Exception:
            detail = resp.text
        print(f"\nERROR: {label} returned HTTP {resp.status_code}: {detail}", file=sys.stderr)
        sys.exit(1)
    return resp.json()


# ── Iteration pretty-printer ───────────────────────────────────────────────────

def _print_iteration(it: dict) -> None:
    n = it.get("iteration_number", "?")
    status = it.get("status", "?")
    outcome = it.get("outcome") or "-"
    score = _fmt_score(it.get("score"))
    dur = _fmt_duration(it.get("started_at"), it.get("finished_at"))
    label = "baseline" if n == 0 else f"iteration {n}"
    print(f"\n  [{label}]  status={status}  outcome={outcome}  score={score}  duration={dur}")

    # Per-task trials
    trials = it.get("trials") or []
    for trial in trials:
        task_id = trial.get("task_id", "?")
        reward = trial.get("reward")
        wall_ms = trial.get("wall_time_ms")
        t_str = f"{wall_ms / 1000:.0f}s" if wall_ms is not None else "-"
        mark = "+" if (reward is not None and float(reward) > 0) else "-"
        print(f"    [{mark}] {task_id}  reward={_fmt_reward(reward)}  t={t_str}")

    # Optimizer artefacts (only present on optimizer-proposed iterations)
    diagnosis = it.get("optimizer_diagnosis")
    expected = it.get("expected_targets")
    transcript_uri = it.get("optimizer_transcript_uri")
    in_tok = it.get("optimizer_input_tokens")
    out_tok = it.get("optimizer_output_tokens")
    cost = it.get("optimizer_cost_usd")

    if diagnosis:
        print(f"    diagnosis: {diagnosis}")
    if expected:
        targets_str = ", ".join(expected) if isinstance(expected, list) else str(expected)
        print(f"    expected targets: {targets_str}")
    if transcript_uri or in_tok is not None or out_tok is not None or cost is not None:
        tok_str = f"{in_tok} in / {out_tok} out tokens" if (in_tok is not None and out_tok is not None) else ""
        cost_str = _fmt_cost(cost)
        uri_part = transcript_uri or ""
        parts = [p for p in [uri_part, tok_str, cost_str] if p and p != "-"]
        print(f"    artefacts: {', '.join(parts)}")

    if it.get("error_message"):
        print(f"    ERROR: {it['error_message']}")


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Submit a benchmark run and poll for results."
    )
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="Base URL of the running service (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--template-id",
        default="terminal_bench",
        help="Template ID to use (default: terminal_bench)",
    )
    parser.add_argument(
        "--max-iterations",
        type=int,
        default=3,
        help="Maximum iterations for the job (default: 3)",
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Experiment name (default: smoke-<timestamp>)",
    )
    parser.add_argument(
        "--task-ids",
        default=None,
        help="Comma-separated task IDs to benchmark (default: template defaults)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="Seconds between status polls (default: 5)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=5400.0,
        help="Total polling timeout in seconds (default: 5400 = 90 min)",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Submit and exit immediately without polling",
    )
    args = parser.parse_args()

    # Auto-generate name if not provided
    if args.name is None:
        args.name = f"smoke-{int(time.time())}"

    # Parse task IDs
    task_ids = None
    if args.task_ids:
        task_ids = [t.strip() for t in args.task_ids.split(",") if t.strip()]

    print("=" * 60)
    print("Agent Optimization Service — Smoke Client")
    print("=" * 60)
    print(f"  Base URL:       {args.base_url}")
    print(f"  Template:       {args.template_id}")
    print(f"  Experiment:     {args.name}")
    print(f"  Max iterations: {args.max_iterations}")
    if task_ids:
        print(f"  Task IDs:       {', '.join(task_ids)}")
    print()

    client = httpx.Client(base_url=args.base_url, timeout=30.0)

    # ── Step 1: Create experiment ──────────────────────────────────────────────
    print("Step 1: Creating experiment...")
    exp_body: dict[str, Any] = {
        "name": args.name,
        "template_id": args.template_id,
    }
    if task_ids:
        exp_body["task_ids"] = task_ids

    resp = client.post("/experiments", json=exp_body)
    exp = _check_response(resp, "POST /experiments")

    experiment_id = exp["id"]
    baseline_av_id = exp.get("baseline_agent_version_id", "-")
    exp_status = exp.get("status", "-")

    print(f"  experiment_id:             {experiment_id}")
    print(f"  baseline_agent_version_id: {baseline_av_id}")
    print(f"  status:                    {exp_status}")

    # ── Step 2: Create job ─────────────────────────────────────────────────────
    print("\nStep 2: Submitting job...")
    job_body: dict[str, Any] = {"max_iterations": args.max_iterations}
    resp = client.post(f"/experiments/{experiment_id}/jobs", json=job_body)
    job = _check_response(resp, f"POST /experiments/{experiment_id}/jobs")

    job_id = job["id"]
    job_status = job.get("status", "-")

    print(f"  job_id:  {job_id}")
    print(f"  status:  {job_status}")

    # ── Step 3: --no-wait early exit ───────────────────────────────────────────
    if args.no_wait:
        print()
        print("--no-wait: exiting without polling.")
        print(f"  To poll manually:  GET {args.base_url}/experiments/{experiment_id}/jobs/{job_id}")
        print(f"  Full history:      GET {args.base_url}/experiments/{experiment_id}/iterations")
        client.close()
        return 0

    # ── Step 4: Poll for completion ────────────────────────────────────────────
    terminal_statuses = {"done", "failed", "cancelled"}
    start_ts = time.time()
    print("\nStep 3: Polling for job completion...")

    while True:
        elapsed = time.time() - start_ts
        if elapsed > args.timeout:
            print(f"\nTIMEOUT after {_elapsed(start_ts)} — job did not finish in time.")
            client.close()
            return 2

        resp = client.get(f"/experiments/{experiment_id}/jobs/{job_id}")
        job = _check_response(resp, f"GET /experiments/{experiment_id}/jobs/{job_id}")
        job_status = job.get("status", "unknown")
        iter_info = ""
        if job.get("started_at_iteration") is not None:
            iter_info = f"  iteration={job['started_at_iteration']}"

        print(f"  [{_elapsed(start_ts)}] status={job_status}{iter_info}", end="\r", flush=True)

        if job_status in terminal_statuses:
            print()  # newline after the \r
            break

        time.sleep(args.poll_interval)

    # ── Step 5: Fetch full iteration history ───────────────────────────────────
    print("\nStep 4: Fetching iteration history...")
    resp = client.get(f"/experiments/{experiment_id}/iterations")
    iterations_summary = _check_response(resp, f"GET /experiments/{experiment_id}/iterations")

    # Fetch full detail for each iteration (includes trials + optimizer fields)
    print(f"\nIteration history ({len(iterations_summary)} iteration(s)):")
    for it_summary in iterations_summary:
        n = it_summary.get("iteration_number")
        resp_detail = client.get(
            f"/experiments/{experiment_id}/iterations/{n}"
        )
        if resp_detail.is_error:
            # Fall back to summary if detail endpoint fails
            _print_iteration(it_summary)
        else:
            _print_iteration(resp_detail.json())

    # ── Step 6: Final summary ──────────────────────────────────────────────────
    # Refresh experiment to get latest best_score
    resp = client.get(f"/experiments/{experiment_id}")
    exp_final = _check_response(resp, f"GET /experiments/{experiment_id}")

    completed_count = exp_final.get("current_iteration_count", len(iterations_summary))
    best_score = _fmt_score(exp_final.get("best_score"))
    stop_reason = job.get("stop_reason") or "-"
    error_msg = job.get("error_message")

    print()
    print("=" * 60)
    print("Final Summary")
    print("=" * 60)
    print(f"  Experiment ID:       {experiment_id}")
    print(f"  Best score:          {best_score}")
    print(f"  Iterations completed:{completed_count}")
    print(f"  Final job status:    {job_status}")
    print(f"  Stop reason:         {stop_reason}")
    if error_msg:
        print(f"  Error:               {error_msg}")
    print(f"  Total elapsed:       {_elapsed(start_ts)}")
    print()

    client.close()

    if job_status == "done":
        print("Job completed successfully.")
        return 0
    else:
        print(f"Job ended with status '{job_status}'.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
