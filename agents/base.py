"""Shared machinery for running one isolated, schema-bound agent.

Each call to :func:`run_structured_agent` runs its own ``query()``, which
starts a separate Claude Code subprocess with a fresh context window. Three
of them under ``asyncio.gather`` therefore share no conversation history.
They only meet when the Supervisor reads their validated outputs.

Guardrails applied to every run:
  * ``max_turns`` (the "max_steps" limit) and ``max_budget_usd``
  * an explicit built-in ``tools`` allowlist plus ``permission_mode="dontAsk"``,
    so any tool not pre-approved is refused instead of prompting
  * ``setting_sources=[]``, so no user/project settings or CLAUDE.md files
    leak into the subagent's context
  * the PreToolUse/PostToolUse audit + write-guard hooks from ``hooks.py``
  * ``output_format`` = the Pydantic model's JSON Schema, then
    ``model_validate`` again on our side
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)
from pydantic import BaseModel, ValidationError

from hooks import PROJECT_ROOT, TraceLogger

T = TypeVar("T", bound=BaseModel)

SUPERVISOR_MODEL = os.getenv("WC_SUPERVISOR_MODEL", "claude-opus-5-5")
SUBAGENT_MODEL = os.getenv("WC_SUBAGENT_MODEL", "claude-sonnet-5-5")
DEFAULT_MAX_TURNS = int(os.getenv("WC_SUBAGENT_MAX_TURNS", "12"))
DEFAULT_BUDGET_USD = float(os.getenv("WC_SUBAGENT_BUDGET_USD", "0.75"))


class SubagentError(RuntimeError):
    """A subagent failed, hit a limit, or returned output that didn't validate."""

    def __init__(self, agent: str, message: str) -> None:
        super().__init__(f"[{agent}] {message}")
        self.agent = agent


@dataclass
class AgentSpec:
    """Everything needed to run one specialised agent."""

    name: str
    system_prompt: str
    output_model: type[BaseModel]
    builtin_tools: list[str] = field(default_factory=list)
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    mcp_tool_names: list[str] = field(default_factory=list)
    model: str = SUBAGENT_MODEL
    max_turns: int = DEFAULT_MAX_TURNS
    max_budget_usd: float = DEFAULT_BUDGET_USD


# Lets tests (and ``main.py --demo``) swap the real SDK call for a fake.
AgentRunner = Callable[[AgentSpec, str, TraceLogger], Awaitable[BaseModel]]


def build_options(spec: AgentSpec, trace: TraceLogger) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=spec.model,
        system_prompt=spec.system_prompt,
        tools=spec.builtin_tools,
        allowed_tools=[*spec.builtin_tools, *spec.mcp_tool_names],
        mcp_servers=spec.mcp_servers,
        permission_mode="dontAsk",
        max_turns=spec.max_turns,
        max_budget_usd=spec.max_budget_usd,
        setting_sources=[],
        cwd=str(PROJECT_ROOT),
        hooks=trace.hooks_for(spec.name),
        output_format={"type": "json_schema", "schema": spec.output_model.model_json_schema()},
    )


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*\})\s*```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Fallback parser for when ``structured_output`` is empty: try fenced JSON, then the outermost {...}."""
    if m := _JSON_BLOCK.search(text):
        return json.loads(m.group(1))
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in agent output")
    return json.loads(text[start : end + 1])


def validate_output(agent: str, model: type[T], payload: Any) -> T:
    try:
        if isinstance(payload, str):
            payload = extract_json(payload)
        return model.model_validate(payload)
    except (ValidationError, ValueError) as exc:
        raise SubagentError(agent, f"output failed {model.__name__} validation: {exc}") from exc


async def run_structured_agent(spec: AgentSpec, prompt: str, trace: TraceLogger) -> BaseModel:
    """Run one agent to completion and return its validated output model."""
    trace.write("agent_start", agent=spec.name, model=spec.model, max_turns=spec.max_turns)
    result: ResultMessage | None = None
    last_text = ""

    async for message in query(prompt=prompt, options=build_options(spec, trace)):
        if isinstance(message, AssistantMessage):
            texts = [b.text for b in message.content if isinstance(b, TextBlock)]
            if texts:
                last_text = "\n".join(texts)
        elif isinstance(message, ResultMessage):
            result = message

    if result is None:
        raise SubagentError(spec.name, "stream ended without a ResultMessage")
    trace.log_usage(spec.name, result)

    if result.is_error or result.subtype != "success":
        reason = result.terminal_reason or result.subtype
        raise SubagentError(spec.name, f"run ended with {reason!r}: {result.errors or result.result}")

    payload = result.structured_output
    if payload is None:
        payload = result.result or last_text
    return validate_output(spec.name, spec.output_model, payload)
