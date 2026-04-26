# HarnessAgent for Terminal-Bench 2.0 — starting template.
import json
import os

import litellm
from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

MAX_STEPS = 80
MAX_OUTPUT_CHARS = 8000
MODEL = os.environ.get("AGENT_MODEL", "gpt-5.4")

AGENT_INSTRUCTION = """\
You are an autonomous terminal agent. You are given a task and a Linux container.
You solve tasks by executing bash commands.

## Mandatory Workflow

### Step 1 — Bootstrap (always first)
Run a single command to orient yourself:
```
uname -a && python3 --version && pwd && ls -la
```

### Step 2 — Understand the Task
Before writing ANY code:
1. Restate the task in your own words (in your thinking)
2. Identify EXACTLY what the task is asking for — be precise about scope, data, metric, and output format
3. Read any task files, READMEs, or data files that exist in /app or /task
4. Write a numbered plan of steps you will execute

### Step 3 — Execute your Plan
- Follow your plan step by step
- Check command output for errors before proceeding to the next step
- If a step fails, diagnose and fix before moving on
- Install missing dependencies as needed (use pip install -q)

### Step 4 — Verify (mandatory)
Before writing the final answer:
1. Double-check your result using a DIFFERENT method or approach
2. Sanity-check: does the result look reasonable? Is the magnitude correct?
3. Re-read the original task to confirm you answered the right question

### Step 5 — Write Answer and Finish
- Write the final answer to the required output file (check task for exact path/format)
- Send a final text message (no tool call) summarizing what you did and your answer

## Rules
- Never ask questions — just act
- Never guess at task requirements — read the task files first
- If counting/computing something: try MULTIPLE methods and compare results. If they differ, investigate why before choosing.
- Check output for errors at each step
- Be precise about scope: do not filter data unless the task explicitly says to filter
- When counting tokens in a dataset: count ALL relevant text fields the task refers to (e.g., if asked about "deepseek tokens", include both the thinking/reasoning AND output/solution fields, not just one)
- When tokenizing text: try both add_special_tokens=True and add_special_tokens=False and choose the one that matches the task's intent (use True if the tokens are meant to be model-ready)
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Execute a bash command in the container. Returns stdout and stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "analysis": {
                        "type": "string",
                        "description": "Brief analysis of what you observed so far and what this command will do.",
                    },
                    "plan": {
                        "type": "string",
                        "description": "The next 1-3 steps you plan to take after this command.",
                    },
                    "command": {
                        "type": "string",
                        "description": "The bash command to execute.",
                    }
                },
                "required": ["command"],
            },
        },
    }
]


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Truncate long output, keeping the beginning and end."""
    if not text or len(text) <= limit:
        return text or ""
    half = limit // 2
    return (
        text[:half]
        + f"\n\n... [{len(text) - limit} chars truncated] ...\n\n"
        + text[-half:]
    )


class HarnessAgent(BaseAgent):
    """Agent under optimization for Terminal-Bench 2.0."""

    @staticmethod
    def name() -> str:
        return "harness-agent"

    def version(self) -> str | None:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        pass

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        model = self.model_name or MODEL
        total_input_tokens = 0
        total_output_tokens = 0

        messages = [
            {"role": "system", "content": AGENT_INSTRUCTION},
            {"role": "user", "content": f"Task:\n{instruction}"},
        ]

        for step in range(MAX_STEPS):
            try:
                response = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    tools=TOOLS,
                    tool_choice="auto",
                )
            except Exception as e:
                self.logger.error(f"LLM call failed at step {step}: {e}")
                break

            usage = response.usage
            if usage:
                total_input_tokens += usage.prompt_tokens or 0
                total_output_tokens += usage.completion_tokens or 0

            choice = response.choices[0]
            message = choice.message

            # Build the assistant message for history
            assistant_msg = {"role": "assistant", "content": message.content}
            if message.tool_calls:
                assistant_msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in message.tool_calls
                ]
            messages.append(assistant_msg)

            # If the model returned text without tool calls → task complete
            if not message.tool_calls:
                self.logger.info(f"Agent declared complete at step {step}")
                break

            # Execute each tool call
            for tc in message.tool_calls:
                if tc.function.name != "bash":
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": f"Unknown tool: {tc.function.name}",
                    })
                    continue

                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": "Error: invalid JSON arguments",
                    })
                    continue

                command = args.get("command", "")
                self.logger.info(f"Step {step} | bash: {command[:200]}")

                result = await environment.exec(command, timeout_sec=120)

                output_parts = []
                if result.stdout:
                    output_parts.append(result.stdout)
                if result.stderr:
                    output_parts.append(f"STDERR:\n{result.stderr}")
                if result.return_code != 0:
                    output_parts.append(f"[exit code: {result.return_code}]")

                output = "\n".join(output_parts) if output_parts else "(no output)"
                output = _truncate(output)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": output,
                })

        # Save full conversation trace for failure analysis (disabled for test splits)
        if os.environ.get("HARNESS_SAVE_TRACE", "1") == "1":
            trace_path = self.logs_dir / "trace.json"
            try:
                with open(trace_path, "w") as f:
                    json.dump(messages, f, indent=2, default=str)
                self.logger.info(f"Trace saved to {trace_path}")
            except Exception as e:
                self.logger.warning(f"Failed to save trace: {e}")

        # Populate context
        context.n_input_tokens = total_input_tokens
        context.n_output_tokens = total_output_tokens
