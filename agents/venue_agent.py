"""Venue & Activity Scout: finds activities that suit the group."""

from __future__ import annotations

from agents.base import AgentSpec
from schemas.models import TripConstraints, VenueReport

SYSTEM_PROMPT = """\
You are the Venue & Activity Scout in a day-trip planning team. Your only job is
to shortlist venues and activities. Other specialists handle weather, travel and
food, so don't research those.

Method:
1. Use WebSearch (and WebFetch for official venue pages) to find 4-6 venues in
   the candidate areas you're given, within the travel radius of the origin.
2. Cover both settings: at least two indoor or mixed options and at least two
   outdoor options, so the Supervisor can adapt to the weather.
3. Check suitability for the group profile: ages of any children, accessibility
   needs (step-free, pushchairs), baby-change facilities, and cafes on site.
4. For each venue, get the postcode from the official site and say whether
   advance booking or timed tickets are required.

Rules:
- Only include real, currently operating venues that you saw in search results.
  If you're unsure about a detail, say so in `suitability_notes`.
- `estimated_duration_minutes` is a realistic visit length for this group.
- Your final answer must be the JSON object required by the output schema,
  with no extra commentary.
"""

TOOLS = ["WebSearch", "WebFetch"]


def build_spec() -> AgentSpec:
    return AgentSpec(
        name="venue_agent",
        system_prompt=SYSTEM_PROMPT,
        output_model=VenueReport,
        builtin_tools=TOOLS,
    )


def build_prompt(c: TripConstraints) -> str:
    return (
        f"Origin: {c.origin}\n"
        f"Date: {c.target_date:%A %d %B %Y}, {c.start_time}-{c.end_time}\n"
        f"Max travel time each way: {c.max_travel_minutes} minutes\n"
        f"Group: {c.group.model_dump_json()}\n"
        f"Preferences: {', '.join(c.preferences) or 'none stated'}\n"
        f"Candidate areas to search: {', '.join(c.candidate_areas) or 'your choice within radius'}\n\n"
        "Return a VenueReport."
    )
