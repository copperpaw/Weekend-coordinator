"""Deterministic lifecycle hooks and the JSONL audit trail.

* ``PreToolUse``  - logs every tool call and **denies** file writes outside
  ``./outputs/``. This is a code-level guardrail, not a prompt instruction.
* ``PostToolUse`` - logs how long the tool took (``duration_ms`` from the CLI,
  or our own timing if the CLI doesn't report it).
* ``TraceLogger.log_usage`` - token usage and cost per agent run. These come
  from ``ResultMessage``, not from tool hooks, but go into the same file.

Every record goes to ``logs/agent_trace.jsonl``, one JSON object per line.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookContext, HookMatcher

PROJECT_ROOT = Path(__file__).resolve().parent
OUTPUTS_DIR = Path(os.getenv("WC_OUTPUTS_DIR", PROJECT_ROOT / "outputs")).resolve()
TRACE_PATH = Path(os.getenv("WC_TRACE_PATH", PROJECT_ROOT / "logs" / "agent_trace.jsonl"))

# Built-in tools that write files, and the input key holding the target path.
WRITE_TOOLS: dict[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
# Shell constructs that write to a file: redirection, tee, cp/mv/touch etc.
_BASH_REDIRECT = re.compile(r"(?<![0-9&])>>?\s*(?P<target>[^\s;&|]+)")
_BASH_WRITE_CMDS = {"tee", "cp", "mv", "touch", "mkdir", "rm", "dd", "install"}


def is_inside_outputs(path: str | os.PathLike[str], cwd: str | None = None) -> bool:
    """True if ``path`` resolves (following ``..`` and symlinks) inside OUTPUTS_DIR."""
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path(cwd or os.getcwd()) / p
    resolved = p.resolve()
    return resolved == OUTPUTS_DIR or OUTPUTS_DIR in resolved.parents


def bash_write_targets(command: str) -> list[str]:
    """Best-effort extraction of the paths a shell command would write to."""
    targets = [m.group("target") for m in _BASH_REDIRECT.finditer(command)]
    try:
        tokens = shlex.split(command)
    except ValueError:
        return targets + ["<unparseable command>"]
    for i, tok in enumerate(tokens):
        if tok in _BASH_WRITE_CMDS:
            # Every non-flag argument after the command could be a target.
            for arg in tokens[i + 1 :]:
                if arg in {"|", ";", "&&", "||"}:
                    break
                if not arg.startswith("-"):
                    targets.append(arg)
    return [t for t in targets if t not in {"/dev/null", "/dev/stderr", "/dev/stdout"}]


class TraceLogger:
    """Thread-safe JSONL appender, shared by every hook and agent run."""

    def __init__(self, path: Path = TRACE_PATH, run_id: str | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        self._lock = threading.Lock()
        self._starts: dict[str, float] = {}

    def write(self, event: str, **fields: Any) -> None:
        record = {"ts": datetime.now(UTC).isoformat(), "run_id": self.run_id, "event": event}
        record.update(fields)
        line = json.dumps(record, default=str)
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def log_usage(self, agent: str, result: Any) -> None:
        """Record token usage and cost from a ``ResultMessage``."""
        self.write(
            "agent_result",
            agent=agent,
            subtype=getattr(result, "subtype", None),
            is_error=getattr(result, "is_error", None),
            num_turns=getattr(result, "num_turns", None),
            duration_ms=getattr(result, "duration_ms", None),
            total_cost_usd=getattr(result, "total_cost_usd", None),
            usage=getattr(result, "usage", None),
            model_usage=getattr(result, "model_usage", None),
            terminal_reason=getattr(result, "terminal_reason", None),
        )

    # -- hook callbacks -----------------------------------------------------

    def hooks_for(self, agent: str) -> dict[str, list[HookMatcher]]:
        """Build the ``hooks=`` mapping for one agent, tagged with its name."""

        async def pre_tool_use(
            input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
        ) -> dict[str, Any]:
            tool = input_data.get("tool_name", "")
            tool_input = input_data.get("tool_input", {}) or {}
            if tool_use_id:
                self._starts[tool_use_id] = time.perf_counter()

            denial = self.check_write_policy(tool, tool_input, input_data.get("cwd"))
            self.write(
                "pre_tool_use",
                agent=agent,
                tool=tool,
                tool_use_id=tool_use_id,
                tool_input=_truncate(tool_input),
                decision="deny" if denial else "allow",
                reason=denial,
            )
            if denial:
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": denial,
                    }
                }
            return {}

        async def post_tool_use(
            input_data: dict[str, Any], tool_use_id: str | None, context: HookContext
        ) -> dict[str, Any]:
            started = self._starts.pop(tool_use_id or "", None)
            measured = round((time.perf_counter() - started) * 1000) if started else None
            self.write(
                "post_tool_use",
                agent=agent,
                tool=input_data.get("tool_name"),
                tool_use_id=tool_use_id,
                duration_ms=input_data.get("duration_ms", measured),
                response_preview=_truncate(input_data.get("tool_response"), 400),
            )
            return {}

        return {
            "PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use])],
            "PostToolUse": [HookMatcher(matcher=None, hooks=[post_tool_use])],
        }

    @staticmethod
    def check_write_policy(tool: str, tool_input: dict[str, Any], cwd: str | None) -> str | None:
        """Return a denial reason, or ``None`` if the call is allowed."""
        if tool in WRITE_TOOLS:
            target = tool_input.get(WRITE_TOOLS[tool])
            if not target or not is_inside_outputs(target, cwd):
                return f"{tool} blocked: writes are only permitted inside {OUTPUTS_DIR}"
        elif tool == "Bash":
            bad = [
                t
                for t in bash_write_targets(str(tool_input.get("command", "")))
                if not is_inside_outputs(t, cwd)
            ]
            if bad:
                return f"Bash blocked: would write outside {OUTPUTS_DIR}: {bad}"
        return None


def _truncate(value: Any, limit: int = 1000) -> Any:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "…"
