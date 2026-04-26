"""
worker/outer_agent/base.py — Abstract interface for the outer (optimizer) agent.

See design.md §5.14.  All concrete implementations must subclass OuterAgent
and return an OuterAgentResult regardless of internal error handling.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class OuterAgentResult:
    """
    Return value from every OuterAgent.run() call.

    On success: status='ok', new_agent_py / new_learnings_md contain the
    updated file text, transcript holds every message & tool call.

    On failure: status='error' or 'timeout'.  The caller (optimizer handler)
    checks status first; if not 'ok' it marks the iteration failed and stops
    without enqueueing a next benchmark run.
    """

    new_agent_py: str
    new_learnings_md: str
    transcript: list[dict]          # ordered LLM + tool calls, JSON-serialisable
    diagnosis: str                  # 1-3 sentence summary of what the agent did
    expected_targets: list[str]     # task_ids the agent expects to improve
    input_tokens: int
    output_tokens: int
    cost_usd: float | None
    wall_time_ms: int
    status: Literal["ok", "error", "timeout"]
    error_message: str | None = None


class OuterAgent(ABC):
    """
    Strategy interface for the optimizer agent.

    The worker creates a pre-populated scratch_dir, calls run(), then reads
    back the result artefacts from the returned OuterAgentResult — it never
    reads scratch_dir after the call returns.

    Contract:
    - Must never raise.  Return status='error' with error_message instead.
    - Must respect timeout_seconds as a hard cap on wall time.
    - Must not write files outside scratch_dir.
    """

    @abstractmethod
    async def run(
        self,
        scratch_dir: Path,
        instructions: str,
        model: str,
        timeout_seconds: int,
    ) -> OuterAgentResult:
        """
        Run the agentic optimization session.

        Args:
            scratch_dir:      Pre-populated with agent.py, learnings.md,
                              traces/<task>/, and INSTRUCTIONS.md.
            instructions:     Contents of INSTRUCTIONS.md (also already on
                              disk; passed here for convenience).
            model:            Model identifier, e.g. 'claude-sonnet-4-6'.
            timeout_seconds:  Hard wall-time cap for the entire session.

        Returns:
            OuterAgentResult with status in {'ok', 'error', 'timeout'}.
        """
        ...
