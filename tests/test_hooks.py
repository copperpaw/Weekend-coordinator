from __future__ import annotations

import json

import pytest

from hooks import OUTPUTS_DIR, PROJECT_ROOT, TraceLogger, bash_write_targets, is_inside_outputs


@pytest.mark.parametrize(
    ("path", "ok"),
    [
        (OUTPUTS_DIR / "plan.md", True),
        ("outputs/sub/plan.md", True),
        ("outputs/../hooks.py", False),
        ("/etc/passwd", False),
        ("config/preferences.yaml", False),
    ],
)
def test_is_inside_outputs(path, ok):
    assert is_inside_outputs(path, cwd=str(PROJECT_ROOT)) is ok


@pytest.mark.parametrize(
    ("tool", "tool_input", "denied"),
    [
        ("Write", {"file_path": "outputs/x.md"}, False),
        ("Write", {"file_path": "hooks.py"}, True),
        ("Edit", {"file_path": "/tmp/x"}, True),
        ("NotebookEdit", {"notebook_path": "outputs/n.ipynb"}, False),
        ("Bash", {"command": "echo hi > outputs/a.txt"}, False),
        ("Bash", {"command": "echo hi > ~/.bashrc"}, True),
        ("Bash", {"command": "curl x | tee config/preferences.yaml"}, True),
        ("Bash", {"command": "ls -la 2>/dev/null"}, False),
        ("WebSearch", {"query": "museums"}, False),
    ],
)
def test_write_policy(tool, tool_input, denied):
    reason = TraceLogger.check_write_policy(tool, tool_input, str(PROJECT_ROOT))
    assert (reason is not None) is denied


def test_bash_write_targets():
    assert bash_write_targets("cp a.txt /etc/b.txt") == ["a.txt", "/etc/b.txt"]
    assert bash_write_targets("cat x >> log.txt") == ["log.txt"]


async def test_hooks_deny_and_log(tmp_path):
    trace = TraceLogger(path=tmp_path / "t.jsonl", run_id="r")
    hooks = trace.hooks_for("food_agent")
    pre, post = hooks["PreToolUse"][0].hooks[0], hooks["PostToolUse"][0].hooks[0]

    denied = await pre(
        {"tool_name": "Write", "tool_input": {"file_path": "/etc/x"}, "cwd": str(PROJECT_ROOT)}, "t1", {}
    )
    assert denied["hookSpecificOutput"]["permissionDecision"] == "deny"

    allowed = await pre({"tool_name": "WebSearch", "tool_input": {"query": "pubs"}}, "t2", {})
    assert allowed == {}
    await post({"tool_name": "WebSearch", "tool_response": "ok", "duration_ms": 42}, "t2", {})

    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    assert [e["event"] for e in events] == ["pre_tool_use", "pre_tool_use", "post_tool_use"]
    assert events[0]["decision"] == "deny" and events[0]["agent"] == "food_agent"
    assert events[2]["duration_ms"] == 42


def test_log_usage_records_tokens(tmp_path):
    class FakeResult:
        subtype, is_error, num_turns, duration_ms = "success", False, 3, 1200
        total_cost_usd = 0.04
        usage = {"input_tokens": 1000, "output_tokens": 200}
        model_usage = None
        terminal_reason = "completed"

    trace = TraceLogger(path=tmp_path / "t.jsonl")
    trace.log_usage("venue_agent", FakeResult())
    rec = json.loads(trace.path.read_text())
    assert rec["usage"]["output_tokens"] == 200 and rec["total_cost_usd"] == 0.04
