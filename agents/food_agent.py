"""Dining & Dietary Filter: lunch options that respect the user's preferences."""

from __future__ import annotations

from agents.base import AgentSpec
from schemas.models import FoodReport, TripConstraints
from tools.preferences_tool import TOOL_NAMES as PREFS_TOOLS
from tools.preferences_tool import build_preferences_server

SYSTEM_PROMPT = """\
You are the Dining & Dietary Filter in a day-trip planning team. You find
family-friendly lunch spots and only research food.

Method:
1. Always call `get_preferences` first. Its `excluded_ingredients` and
   `prioritise` lists are binding.
2. Use WebSearch/WebFetch to find 4-6 pubs, cafes or restaurants in or near
   the candidate areas, ideally within walking distance of the main attractions.
3. Read the actual menus. For each option:
   - `menu_highlights`: 3-5 dishes that are fine for this user
   - `high_protein_options`: dishes that are clearly high in protein
   - `flagged_ingredients`: any excluded ingredients that appear in common
     dishes, so the Supervisor can weigh them
4. Set `nearest_venue` and `distance_from_venue_km` when you know which nearby
   attraction it serves.

Rules:
- Never list a highlight that contains an excluded ingredient.
- Only use `update_preferences` if the request explicitly asks to change a
  preference.
- `exclusions_applied` must repeat the exclusions you enforced.
- Your final answer must be the JSON object required by the output schema,
  with no extra commentary.
"""

BUILTIN_TOOLS = ["WebSearch", "WebFetch"]


def build_spec() -> AgentSpec:
    return AgentSpec(
        name="food_agent",
        system_prompt=SYSTEM_PROMPT,
        output_model=FoodReport,
        builtin_tools=BUILTIN_TOOLS,
        mcp_servers={"preferences": build_preferences_server()},
        mcp_tool_names=PREFS_TOOLS,
    )


def build_prompt(c: TripConstraints) -> str:
    return (
        f"Origin: {c.origin}\n"
        f"Date: {c.target_date:%A %d %B %Y}; lunch around 12:00-14:00\n"
        f"Group: {c.group.model_dump_json()}\n"
        f"Preferences from the request: {', '.join(c.preferences) or 'none'}\n"
        f"Candidate areas: {', '.join(c.candidate_areas) or 'near the origin'}\n\n"
        "Return a FoodReport."
    )
