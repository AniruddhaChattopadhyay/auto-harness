"""
worker/outer_agent/registry.py — Outer agent registry.

The experiment.optimizer_kind column selects the implementation at runtime.
Add new OuterAgent subclasses here as they're implemented.
"""

from __future__ import annotations

from worker.outer_agent.base import OuterAgent
from worker.outer_agent.claude_agent_sdk import ClaudeAgentSDKOuterAgent

# Maps optimizer_kind string → concrete OuterAgent class.
OUTER_AGENT_REGISTRY: dict[str, type[OuterAgent]] = {
    "claude_agent_sdk": ClaudeAgentSDKOuterAgent,
}


def make_outer_agent(kind: str) -> OuterAgent:
    """
    Instantiate an OuterAgent by registry key.

    Raises:
        KeyError: if *kind* is not registered.  Caller should catch and 400.
    """
    cls = OUTER_AGENT_REGISTRY[kind]  # raises KeyError if unknown
    return cls()
