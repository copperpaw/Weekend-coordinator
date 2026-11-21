"""In-process MCP server exposing ``config/preferences.yaml``.

Tools (as Claude sees them):
  * ``mcp__preferences__get_preferences``
  * ``mcp__preferences__update_preferences``

The Supervisor and the Food Agent receive this server. The file-level
functions (``load_preferences`` / ``apply_update``) are pure and unit-tested
directly.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

PREFS_PATH = Path(
    os.getenv("WC_PREFERENCES_PATH", Path(__file__).resolve().parent.parent / "config" / "preferences.yaml")
)
SERVER_NAME = "preferences"
TOOL_NAMES = [f"mcp__{SERVER_NAME}__get_preferences", f"mcp__{SERVER_NAME}__update_preferences"]

_LIST_FIELDS = {"excluded_ingredients", "prioritise"}
_lock = threading.Lock()


def load_preferences(path: Path | None = None) -> dict[str, Any]:
    path = path or PREFS_PATH
    if not path.exists():
        return {"dietary": {"excluded_ingredients": [], "prioritise": []}, "planning": {}}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def save_preferences(prefs: dict[str, Any], path: Path | None = None) -> None:
    path = path or PREFS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(prefs, sort_keys=False, allow_unicode=True), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX


def apply_update(prefs: dict[str, Any], field: str, action: str, value: str) -> dict[str, Any]:
    """Return a copy of ``prefs`` with one change applied.

    ``field`` is ``excluded_ingredients`` / ``prioritise`` (``add``/``remove``),
    or a ``planning`` key such as ``rain_threshold_pct`` (``set``).
    """
    prefs = json.loads(json.dumps(prefs))  # deep copy
    value = value.strip()
    if field in _LIST_FIELDS:
        bucket: list[str] = prefs.setdefault("dietary", {}).setdefault(field, [])
        lowered = [v.lower() for v in bucket]
        if action == "add":
            if value.lower() not in lowered:
                bucket.append(value)
        elif action == "remove":
            prefs["dietary"][field] = [v for v in bucket if v.lower() != value.lower()]
        else:
            raise ValueError(f"action must be add/remove for {field}")
    elif field == "notes" and action == "set":
        prefs.setdefault("dietary", {})["notes"] = value
    elif action == "set":
        planning = prefs.setdefault("planning", {})
        if field not in planning:
            raise ValueError(f"unknown planning key: {field}")
        current = planning[field]
        if isinstance(current, bool):
            planning[field] = value.lower() in {"true", "yes", "1"}
        elif isinstance(current, int | float):
            planning[field] = type(current)(float(value))
        else:
            planning[field] = value
    else:
        raise ValueError(f"unsupported update: {field}/{action}")
    return prefs


def _text(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=2)
    result: dict[str, Any] = {"content": [{"type": "text", "text": body}]}
    if is_error:
        result["is_error"] = True
    return result


@tool(
    "get_preferences",
    "Read the user's persistent dietary exclusions, priorities and planning thresholds.",
    {"type": "object", "properties": {}, "additionalProperties": False},
)
async def get_preferences(args: dict[str, Any]) -> dict[str, Any]:
    return _text(load_preferences())


@tool(
    "update_preferences",
    "Persistently update preferences.yaml. Use field=excluded_ingredients|prioritise with "
    "action=add|remove, or a planning key (rain_threshold_pct, max_lunch_distance_km, "
    "prefer_public_transport) or 'notes' with action=set.",
    {
        "type": "object",
        "properties": {
            "field": {"type": "string"},
            "action": {"type": "string", "enum": ["add", "remove", "set"]},
            "value": {"type": "string"},
        },
        "required": ["field", "action", "value"],
        "additionalProperties": False,
    },
)
async def update_preferences(args: dict[str, Any]) -> dict[str, Any]:
    with _lock:
        try:
            updated = apply_update(load_preferences(), args["field"], args["action"], args["value"])
        except (ValueError, KeyError) as exc:
            return _text(f"Update rejected: {exc}", is_error=True)
        save_preferences(updated)
    return _text({"status": "saved", "preferences": updated})


def build_preferences_server():
    return create_sdk_mcp_server(
        name=SERVER_NAME, version="1.0.0", tools=[get_preferences, update_preferences]
    )
