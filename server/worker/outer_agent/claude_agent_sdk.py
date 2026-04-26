"""
worker/outer_agent/claude_agent_sdk.py — Claude Agent SDK implementation.

See design.md §5.12 and §5.14.

The Claude Agent SDK (claude-agent-sdk on pip) exposes a query() async
generator that streams messages from a Claude Code session.  We configure it
with:

  - cwd = scratch_dir (the agent's working directory)
  - model = caller-supplied
  - permission_mode = 'acceptEdits' so file edits are auto-approved within
    the allowed tool surface
  - can_use_tool callback enforces the restricted tool surface:
      Read  → allowed for any path under scratch_dir
      Edit  → allowed only for agent.py and learnings.md
      Write → allowed only for agent.py and learnings.md
      Everything else → denied

Deviations from the brief
--------------------------
The brief described a native "filename allowlist" feature in the SDK.  The
actual SDK (as of the installed version) has no built-in filename allowlist.
Instead it exposes a `can_use_tool` async callback on ClaudeAgentOptions that
receives (tool_name, tool_input, ToolPermissionContext) and returns either
PermissionResultAllow or PermissionResultDeny.  We implement the restriction
there.

The `allowed_tools` field on ClaudeAgentOptions is a list of tool name
strings.  Setting it to ['Read', 'Edit', 'Write'] restricts the *set* of
tools Claude is told about; the can_use_tool callback then enforces the per-
filename restriction within that set.

Token usage comes from AssistantMessage.usage dict (keys: 'input_tokens',
'output_tokens') and final cost from ResultMessage.total_cost_usd.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import time
from pathlib import Path
from typing import Any

import structlog

from claude_agent_sdk import query
from claude_agent_sdk.types import (
    AssistantMessage,
    ClaudeAgentOptions,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
    UserMessage,
)

from worker.outer_agent.base import OuterAgent, OuterAgentResult

log = structlog.get_logger(__name__)

# Tools that Claude is allowed to *see* in its tool surface.
_ALLOWED_TOOL_NAMES: list[str] = ["Read", "Edit", "Write"]

# Files the optimizer may modify.  (Read is allowed for any path in scratch.)
_WRITABLE_FILENAMES: frozenset[str] = frozenset({"agent.py", "learnings.md"})


def _is_under(path: str | Path, base: Path) -> bool:
    """Return True if *path* is strictly under *base* (resolves symlinks)."""
    try:
        Path(path).resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def _make_can_use_tool(scratch_dir: Path):
    """
    Build the can_use_tool callback for this session.

    Policy:
      Read  → allowed if the target path is under scratch_dir
      Edit  → allowed if the target file basename is in _WRITABLE_FILENAMES
              AND the path is under scratch_dir
      Write → same as Edit
      Any other tool → denied
    """

    async def can_use_tool(
        tool_name: str,
        tool_input: dict[str, Any],
        ctx: ToolPermissionContext,
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name == "Read":
            target = tool_input.get("file_path") or tool_input.get("path") or ""
            if target and _is_under(target, scratch_dir):
                return PermissionResultAllow()
            return PermissionResultDeny(
                message=f"Read is only allowed within {scratch_dir}",
            )

        if tool_name in ("Edit", "Write"):
            target = (
                tool_input.get("file_path")
                or tool_input.get("path")
                or tool_input.get("new_path")
                or ""
            )
            basename = Path(target).name if target else ""
            if (
                target
                and basename in _WRITABLE_FILENAMES
                and _is_under(target, scratch_dir)
            ):
                return PermissionResultAllow()
            return PermissionResultDeny(
                message=(
                    f"{tool_name} is only allowed on agent.py and learnings.md "
                    f"within {scratch_dir}; got: {target!r}"
                ),
            )

        # All other tools (Bash, WebFetch, etc.) are denied.
        return PermissionResultDeny(
            message=f"Tool '{tool_name}' is not permitted for the optimizer agent.",
        )

    return can_use_tool


def _message_to_dict(msg: Any) -> dict | None:
    """Convert an SDK message to a JSON-serialisable dict for the transcript."""
    if isinstance(msg, AssistantMessage):
        content_items = []
        for block in msg.content:
            if isinstance(block, TextBlock):
                content_items.append({"type": "text", "text": block.text})
            elif isinstance(block, ToolUseBlock):
                content_items.append(
                    {
                        "type": "tool_use",
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    }
                )
            else:
                # Fallback: try __dict__ or str
                try:
                    content_items.append(dataclasses.asdict(block))  # type: ignore[arg-type]
                except Exception:
                    content_items.append({"type": "unknown", "repr": str(block)})
        return {
            "role": "assistant",
            "model": msg.model,
            "content": content_items,
            "usage": msg.usage,
            "stop_reason": msg.stop_reason,
        }

    if isinstance(msg, UserMessage):
        content = msg.content
        if isinstance(content, list):
            content_items = []
            for block in content:
                try:
                    content_items.append(dataclasses.asdict(block))  # type: ignore[arg-type]
                except Exception:
                    content_items.append({"type": "unknown", "repr": str(block)})
            content = content_items
        return {"role": "user", "content": content}

    if isinstance(msg, ResultMessage):
        return {
            "role": "result",
            "subtype": msg.subtype,
            "duration_ms": msg.duration_ms,
            "is_error": msg.is_error,
            "num_turns": msg.num_turns,
            "stop_reason": msg.stop_reason,
            "total_cost_usd": msg.total_cost_usd,
            "usage": msg.usage,
            "result": msg.result,
        }

    # SystemMessage or other — include as best-effort
    try:
        return dataclasses.asdict(msg)  # type: ignore[arg-type]
    except Exception:
        return {"type": type(msg).__name__, "repr": str(msg)}


def _extract_diagnosis(transcript: list[dict]) -> str:
    """
    Extract a 1-3 sentence diagnosis from the transcript.

    Strategy: find the last assistant text block that is non-empty and
    truncate to 3 sentences (split on '. ').  Falls back to a generic message.
    """
    for msg in reversed(transcript):
        if msg.get("role") == "assistant":
            for block in msg.get("content", []):
                if block.get("type") == "text":
                    text = block["text"].strip()
                    if text:
                        # Truncate to ~3 sentences
                        sentences = text.split(". ")
                        return ". ".join(sentences[:3]).strip()
    return "Optimizer agent completed session."


class ClaudeAgentSDKOuterAgent(OuterAgent):
    """
    Outer agent implementation using the claude-agent-sdk `query()` API.

    Runs Claude Code as a subprocess via the SDK, constrained to:
      - Read any file under scratch_dir
      - Edit / Write only agent.py and learnings.md
      - No Bash, WebFetch, WebSearch, or network access
    """

    async def run(
        self,
        scratch_dir: Path,
        instructions: str,
        model: str,
        timeout_seconds: int,
    ) -> OuterAgentResult:
        t0 = time.monotonic()

        transcript: list[dict] = []
        total_input_tokens: int = 0
        total_output_tokens: int = 0
        cost_usd: float | None = None
        result_message: ResultMessage | None = None

        try:
            # Write INSTRUCTIONS.md into scratch_dir so Claude can read it.
            instructions_file = scratch_dir / "INSTRUCTIONS.md"
            instructions_file.write_text(instructions, encoding="utf-8")

            # Ensure learnings.md exists (may be empty for iteration 0).
            learnings_file = scratch_dir / "learnings.md"
            if not learnings_file.exists():
                learnings_file.write_text("", encoding="utf-8")

            options = ClaudeAgentOptions(
                model=model,
                cwd=str(scratch_dir),
                # Restrict which tools Claude is told about.
                allowed_tools=_ALLOWED_TOOL_NAMES,
                # Auto-accept file edits; the can_use_tool callback is our
                # real gate — this avoids interactive prompts.
                permission_mode="acceptEdits",
                # Runtime permission enforcement (filename allowlist).
                can_use_tool=_make_can_use_tool(scratch_dir),
                # Do not inherit project / user settings — clean slate.
                setting_sources=[],
                # Pass ANTHROPIC_API_KEY through environment.
                env={"ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "")},
            )

            prompt_text = (
                "You are the optimizer agent.  Read INSTRUCTIONS.md for your full "
                "task description.  When you are done, stop."
            )

            # The SDK's can_use_tool callback requires streaming-mode input
            # (prompt as AsyncIterable[dict]), not a plain string. Wrap our single
            # user message in an async generator yielding the SDK's message format.
            async def _prompt_stream():
                yield {
                    "type": "user",
                    "message": {"role": "user", "content": prompt_text},
                }

            async def _run_query() -> None:
                nonlocal total_input_tokens, total_output_tokens, cost_usd, result_message
                async for msg in query(prompt=_prompt_stream(), options=options):
                    entry = _message_to_dict(msg)
                    if entry is not None:
                        transcript.append(entry)
                    if isinstance(msg, AssistantMessage) and msg.usage:
                        total_input_tokens += msg.usage.get("input_tokens", 0)
                        total_output_tokens += msg.usage.get("output_tokens", 0)
                    if isinstance(msg, ResultMessage):
                        result_message = msg
                        cost_usd = msg.total_cost_usd
                        if msg.usage:
                            # ResultMessage.usage may have cumulative totals.
                            total_input_tokens = msg.usage.get(
                                "input_tokens", total_input_tokens
                            )
                            total_output_tokens = msg.usage.get(
                                "output_tokens", total_output_tokens
                            )

            await asyncio.wait_for(_run_query(), timeout=timeout_seconds)

        except asyncio.TimeoutError:
            wall_ms = int((time.monotonic() - t0) * 1000)
            log.warning(
                "outer_agent.timeout",
                scratch_dir=str(scratch_dir),
                wall_ms=wall_ms,
                timeout_seconds=timeout_seconds,
            )
            # Read whatever was written before timeout.
            new_agent_py = _safe_read(scratch_dir / "agent.py")
            new_learnings_md = _safe_read(scratch_dir / "learnings.md")
            return OuterAgentResult(
                new_agent_py=new_agent_py,
                new_learnings_md=new_learnings_md,
                transcript=transcript,
                diagnosis="Optimizer agent timed out.",
                expected_targets=[],
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                cost_usd=cost_usd,
                wall_time_ms=wall_ms,
                status="timeout",
                error_message=f"Session exceeded timeout of {timeout_seconds}s.",
            )

        except Exception as exc:
            wall_ms = int((time.monotonic() - t0) * 1000)
            log.exception(
                "outer_agent.error",
                scratch_dir=str(scratch_dir),
                exc=str(exc),
            )
            return OuterAgentResult(
                new_agent_py=_safe_read(scratch_dir / "agent.py"),
                new_learnings_md=_safe_read(scratch_dir / "learnings.md"),
                transcript=transcript,
                diagnosis="Optimizer agent encountered an error.",
                expected_targets=[],
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                cost_usd=cost_usd,
                wall_time_ms=wall_ms,
                status="error",
                error_message=str(exc),
            )

        wall_ms = int((time.monotonic() - t0) * 1000)

        # Read back the two owned files.
        agent_py_path = scratch_dir / "agent.py"
        learnings_md_path = scratch_dir / "learnings.md"

        if not agent_py_path.exists():
            return OuterAgentResult(
                new_agent_py="",
                new_learnings_md=_safe_read(learnings_md_path),
                transcript=transcript,
                diagnosis="Optimizer agent did not produce agent.py.",
                expected_targets=[],
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                cost_usd=cost_usd,
                wall_time_ms=wall_ms,
                status="error",
                error_message="agent.py missing from scratch_dir after session.",
            )

        new_agent_py = agent_py_path.read_text(encoding="utf-8")
        new_learnings_md = _safe_read(learnings_md_path)

        # Check if the SDK reported an error in the ResultMessage.
        if result_message is not None and result_message.is_error:
            errors = result_message.errors or []
            err_str = "; ".join(errors) if errors else "SDK returned is_error=True"
            log.warning("outer_agent.sdk_error", err=err_str)
            return OuterAgentResult(
                new_agent_py=new_agent_py,
                new_learnings_md=new_learnings_md,
                transcript=transcript,
                diagnosis=_extract_diagnosis(transcript),
                expected_targets=[],
                input_tokens=total_input_tokens,
                output_tokens=total_output_tokens,
                cost_usd=cost_usd,
                wall_time_ms=wall_ms,
                status="error",
                error_message=err_str,
            )

        diagnosis = _extract_diagnosis(transcript)

        log.info(
            "outer_agent.done",
            wall_ms=wall_ms,
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=cost_usd,
        )

        return OuterAgentResult(
            new_agent_py=new_agent_py,
            new_learnings_md=new_learnings_md,
            transcript=transcript,
            diagnosis=diagnosis,
            expected_targets=[],   # The optimizer doesn't self-predict task targets
            input_tokens=total_input_tokens,
            output_tokens=total_output_tokens,
            cost_usd=cost_usd,
            wall_time_ms=wall_ms,
            status="ok",
        )


def _safe_read(path: Path) -> str:
    """Read a file, returning empty string if missing."""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
