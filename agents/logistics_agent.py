"""Weather & Travel Analyst: forecast and journey times."""

from __future__ import annotations

from agents.base import AgentSpec
from schemas.models import LogisticsReport, TripConstraints
from tools.travel_tools import TOOL_NAMES as TRAVEL_TOOLS
from tools.travel_tools import build_travel_server

SYSTEM_PROMPT = """\
You are the Weather & Travel Analyst in a day-trip planning team. You report on
the forecast and on journey times. You don't pick venues or restaurants.

Method:
1. `geocode` the origin, then call `weather_forecast` for the target date.
2. Set `risk_level`:
   - high: rain probability >= 60%, or wind >= 50 km/h, or max temp >= 30C or <= 2C
   - moderate: rain probability 35-59%, or wind 30-49 km/h
   - low: otherwise
   List the triggering factors in `risk_reasons`.
3. For each candidate area, give one `public_transport` window using
   `public_transport_journey` (TfL accepts postcodes or "lat,lon"; date is
   YYYYMMDD, time is HHMM) and one `driving` window, estimated with WebSearch
   or from the distance. Assume Saturday traffic and allow for parking.
4. `suggested_departure` should get the group there shortly after the
   requested start time.

Rules:
- Report numbers taken from the tools. If a tool fails, say so in `notes` and
  give a conservative estimate. Never leave a field blank.
- List the APIs or sites you used in `data_sources`.
- Your final answer must be the JSON object required by the output schema,
  with no extra commentary.
"""

BUILTIN_TOOLS = ["WebSearch"]


def build_spec() -> AgentSpec:
    return AgentSpec(
        name="logistics_agent",
        system_prompt=SYSTEM_PROMPT,
        output_model=LogisticsReport,
        builtin_tools=BUILTIN_TOOLS,
        mcp_servers={"travel": build_travel_server()},
        mcp_tool_names=TRAVEL_TOOLS,
    )


def build_prompt(c: TripConstraints) -> str:
    return (
        f"Origin: {c.origin}\n"
        f"Target date: {c.target_date.isoformat()} ({c.target_date:%A})\n"
        f"Day window: {c.start_time}-{c.end_time}\n"
        f"Max travel time each way: {c.max_travel_minutes} minutes\n"
        f"Candidate areas: {', '.join(c.candidate_areas) or 'nearby towns within radius'}\n\n"
        "Return a LogisticsReport."
    )
