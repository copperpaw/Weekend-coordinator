"""Supervisor: parse, fan out in parallel, resolve conflicts, render.

Pipeline::

    request ──► parse_request (LLM, Opus) ──► TripConstraints
                         │
          ┌──────────────┼──────────────┐      asyncio.gather (isolated contexts)
          ▼              ▼              ▼
     venue_agent   logistics_agent   food_agent    (Sonnet, max_turns, budget)
          └──────────────┼──────────────┘
                         ▼
       resolve_conflicts (pure Python, deterministic, unit-tested)
                         ▼
       render_markdown ──► outputs/<date>-<slug>.md

Conflict resolution is deliberately not left to the LLM: the same reports
always produce the same plan, and every decision is recorded in
``ItineraryPlan.resolution_log``.
"""

from __future__ import annotations

import asyncio
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from agents import food_agent, logistics_agent, venue_agent
from agents.base import (
    SUPERVISOR_MODEL,
    AgentRunner,
    AgentSpec,
    SubagentError,
    run_structured_agent,
)
from hooks import OUTPUTS_DIR, TraceLogger, is_inside_outputs
from schemas.models import (
    DiningOption,
    FoodReport,
    ItineraryPlan,
    LogisticsReport,
    ResolutionNote,
    Setting,
    TransitWindow,
    TransportMode,
    TripConstraints,
    Venue,
    VenueReport,
    WeatherRisk,
)
from tools.preferences_tool import TOOL_NAMES as PREFS_TOOLS
from tools.preferences_tool import build_preferences_server, load_preferences

SUBAGENT_TIMEOUT_S = float(os.getenv("WC_SUBAGENT_TIMEOUT_S", "300"))
LUNCH_MINUTES = 60

PARSER_PROMPT = """\
You are the Supervisor of a weekend-planning team. In this step you only turn
the user's request into structured constraints for your specialists.

- Resolve relative dates ("Saturday", "this weekend") against today's date,
  which is given in the message. "Saturday" means the next upcoming Saturday.
- Default to a 09:30-17:30 day and 45 minutes' travel if not stated.
- "family-friendly" with no detail means 2 adults and 2 children aged 3 and 6.
  Record that assumption in `preferences`.
- Choose 3-5 `candidate_areas` (towns/districts) that are realistically within
  the travel radius of the origin by car or train.
- If the user asks to change a standing preference ("stop excluding X", "add
  mushrooms to my exclusions"), call `update_preferences` before answering.
- Your final answer must be the JSON object required by the output schema.
"""


# ==========================================================================
# Deterministic conflict resolution
# ==========================================================================


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def _singular(term: str) -> str:
    term = _norm(term)
    for suffix in ("oes", "es", "s"):
        if term.endswith(suffix) and len(term) > len(suffix) + 2:
            return term[: -len(suffix)] + ("o" if suffix == "oes" else "")
    return term


def mentions_excluded(text: str, excluded: list[str]) -> list[str]:
    """Return the excluded ingredients mentioned in ``text`` (plural-insensitive)."""
    haystack = _norm(text)
    return [e for e in excluded if _singular(e) and _singular(e) in haystack]


def _areas_match(a: str, b: str) -> bool:
    a, b = _norm(a), _norm(b)
    return bool(a and b) and (a in b or b in a)


def best_transit(area: str, windows: list[TransitWindow], prefer_public: bool) -> TransitWindow | None:
    matches = [w for w in windows if _areas_match(w.destination_area, area)]
    if not matches:
        return None
    if prefer_public:
        public = [w for w in matches if w.mode is TransportMode.PUBLIC_TRANSPORT]
        if public:
            return min(public, key=lambda w: w.duration_minutes)
    return min(matches, key=lambda w: w.duration_minutes)


def _day_minutes(c: TripConstraints) -> int:
    start = datetime.strptime(c.start_time, "%H:%M")
    end = datetime.strptime(c.end_time, "%H:%M")
    return int((end - start).total_seconds() // 60)


def resolve_conflicts(
    constraints: TripConstraints,
    venues: VenueReport | None,
    logistics: LogisticsReport | None,
    food: FoodReport | None,
    preferences: dict[str, Any],
) -> ItineraryPlan:
    """Merge the three reports into one plan. Pure and deterministic."""
    log: list[ResolutionNote] = []
    warnings: list[str] = []
    planning = preferences.get("planning", {})
    dietary = preferences.get("dietary", {})
    rain_threshold = int(planning.get("rain_threshold_pct", 60))
    max_lunch_km = float(planning.get("max_lunch_distance_km", 2.0))
    prefer_public = bool(planning.get("prefer_public_transport", False))
    excluded: list[str] = list(dietary.get("excluded_ingredients", []))
    priorities = [_norm(p) for p in dietary.get("prioritise", [])]

    for name, report in (("Venue", venues), ("Logistics", logistics), ("Food", food)):
        if report is None:
            warnings.append(f"{name} agent failed; this section is incomplete.")

    windows = logistics.transit_windows if logistics else []
    forecast = logistics.forecast if logistics else None

    # -- Rule 1: weather decides the indoor/outdoor ranking -----------------
    bad_weather = bool(
        forecast
        and (
            forecast.risk_level is WeatherRisk.HIGH
            or forecast.precipitation_probability_pct >= rain_threshold
        )
    )
    moderate_weather = bool(forecast and forecast.risk_level is WeatherRisk.MODERATE)
    if bad_weather:
        log.append(
            ResolutionNote(
                rule="weather.indoor_priority",
                detail=f"Weather risk {forecast.risk_level} / rain {forecast.precipitation_probability_pct}% "
                f"(threshold {rain_threshold}%): indoor venues ranked first, outdoor moved to backups.",
            )
        )
    elif moderate_weather:
        log.append(
            ResolutionNote(
                rule="weather.mixed_priority",
                detail="Moderate weather risk: indoor/mixed venues preferred, outdoor kept as an option.",
            )
        )
    elif forecast is None:
        warnings.append("No forecast available; ranking ignores weather. Check before leaving.")

    def weather_rank(v: Venue) -> int:
        if bad_weather:
            return {Setting.INDOOR: 0, Setting.MIXED: 1, Setting.OUTDOOR: 3}[v.setting]
        if moderate_weather:
            return {Setting.INDOOR: 0, Setting.MIXED: 0, Setting.OUTDOOR: 1}[v.setting]
        return 0

    # -- Rule 2 & 3: travel radius and fitting the day ----------------------
    day = _day_minutes(constraints)
    viable: list[tuple[int, int, Venue]] = []
    for idx, v in enumerate(venues.venues if venues else []):
        transit = best_transit(v.area, windows, prefer_public)
        if transit is None:
            if windows:
                warnings.append(f"No travel estimate for {v.name} ({v.area}); time unverified.")
            travel = 0
        else:
            travel = transit.duration_minutes
            if travel > constraints.max_travel_minutes:
                log.append(
                    ResolutionNote(
                        rule="travel.radius_exceeded",
                        detail=f"Dropped {v.name}: {travel} min by {transit.mode.value} "
                        f"exceeds the {constraints.max_travel_minutes} min limit.",
                    )
                )
                continue
        if v.estimated_duration_minutes + 2 * travel + LUNCH_MINUTES > day:
            log.append(
                ResolutionNote(
                    rule="schedule.too_long",
                    detail=f"{v.name} ({v.estimated_duration_minutes} min) plus travel and lunch "
                    f"won't fit a {day} min day; moved to backups.",
                )
            )
            viable.append((weather_rank(v) + 5, idx, v))
            continue
        viable.append((weather_rank(v), idx, v))

    viable.sort(key=lambda t: (t[0], t[1]))  # stable: the scout's order breaks ties
    ranked = [v for _, _, v in viable]
    primary = ranked[0] if ranked else None
    if bad_weather and primary and primary.setting is Setting.OUTDOOR:
        warnings.append("Only outdoor venues survived filtering despite poor weather; pack waterproofs.")
    backups = ranked[1:4]
    if primary is None and venues:
        warnings.append("No venue met every constraint; consider widening the travel radius.")
    if primary and primary.booking_required:
        warnings.append(f"Book {primary.name} in advance. {primary.booking_notes}".strip())

    # -- Rule 4: enforce dietary exclusions again (defence in depth) --------
    clean: list[DiningOption] = []
    for opt in food.options if food else []:
        bad_highlights = {h: mentions_excluded(h, excluded) for h in opt.menu_highlights}
        kept = [h for h, hits in bad_highlights.items() if not hits]
        removed = [h for h, hits in bad_highlights.items() if hits]
        if removed:
            log.append(
                ResolutionNote(
                    rule="diet.highlight_removed",
                    detail=f"{opt.name}: removed {removed}, which contain excluded ingredients.",
                )
            )
        if not kept:
            log.append(
                ResolutionNote(
                    rule="diet.option_dropped",
                    detail=f"Dropped {opt.name}: no compliant menu highlights left.",
                )
            )
            continue
        clean.append(opt.model_copy(update={"menu_highlights": kept}))

    # -- Rule 5: pick lunch near the chosen venue, favouring protein --------
    def dining_score(o: DiningOption) -> tuple[float, ...]:
        near = 0.0
        if primary:
            if o.nearest_venue and _areas_match(o.nearest_venue, primary.name):
                near = 2.0
            elif _areas_match(o.area, primary.area):
                near = 1.0
        too_far = o.distance_from_venue_km is not None and o.distance_from_venue_km > max_lunch_km
        protein = len(o.high_protein_options) if any("protein" in p for p in priorities) else 0
        return (
            -near,
            1.0 if too_far else 0.0,
            float(len(o.flagged_ingredients)),
            -float(protein),
            0.0 if o.family_friendly else 1.0,
            o.distance_from_venue_km if o.distance_from_venue_km is not None else 99.0,
        )

    clean.sort(key=dining_score)
    lunch = clean[0] if clean else None
    if lunch and primary:
        log.append(
            ResolutionNote(
                rule="diet.lunch_pairing",
                detail=f"Lunch at {lunch.name} ({lunch.area}) paired with {primary.name}; "
                f"{len(lunch.high_protein_options)} high-protein option(s).",
            )
        )
    if lunch and lunch.flagged_ingredients:
        flagged = ", ".join(lunch.flagged_ingredients)
        warnings.append(f"{lunch.name}'s menu features {flagged}; order carefully.")
    if lunch and lunch.booking_recommended:
        warnings.append(f"Booking recommended at {lunch.name}.")

    travel = []
    if primary:
        travel = [w for w in windows if _areas_match(w.destination_area, primary.area)]

    return ItineraryPlan(
        constraints=constraints,
        primary_venue=primary,
        backup_venues=backups,
        lunch=lunch,
        backup_dining=clean[1:3],
        travel=sorted(travel, key=lambda w: w.duration_minutes),
        forecast=forecast,
        resolution_log=log,
        warnings=warnings,
    )


# ==========================================================================
# Markdown rendering
# ==========================================================================


def render_markdown(plan: ItineraryPlan) -> str:
    c = plan.constraints
    lines = [
        f"# Day Trip: {c.target_date:%A %d %B %Y}",
        "",
        f"**From:** {c.origin} · **Window:** {c.start_time}-{c.end_time} · "
        f"**Max travel:** {c.max_travel_minutes} min · "
        f"**Group:** {c.group.adults} adults, {c.group.children} children"
        + (f" (ages {', '.join(map(str, c.group.child_ages))})" if c.group.child_ages else ""),
        "",
    ]
    if plan.warnings:
        lines += ["> [!WARNING]", *[f"> - {w}" for w in plan.warnings], ""]

    if f := plan.forecast:
        lines += [
            "## Weather",
            f"{f.summary}. {f.min_temp_c:.0f}-{f.max_temp_c:.0f}°C, "
            f"{f.precipitation_probability_pct}% chance of rain, wind {f.wind_speed_kmh:.0f} km/h. "
            f"**Risk: {f.risk_level.value}**",
            "",
        ]

    if v := plan.primary_venue:
        lines += [
            f"## Main activity: {v.name}",
            f"- **Where:** {v.area}, {v.postcode}",
            f"- **Setting:** {v.setting.value} · **Allow:** ~{v.estimated_duration_minutes} min",
            f"- **Booking:** {'required' if v.booking_required else 'not required'}"
            + (f" ({v.booking_notes})" if v.booking_notes else ""),
        ]
        if v.approx_cost_gbp is not None:
            lines.append(f"- **Approx. cost:** £{v.approx_cost_gbp:.2f}")
        if v.suitability_notes:
            lines.append(f"- **Why:** {v.suitability_notes}")
        if v.url:
            lines.append(f"- {v.url}")
        lines.append("")

    if plan.travel:
        lines += ["## Getting there", "| Mode | Depart | Duration | Route |", "|---|---|---|---|"]
        for w in plan.travel:
            lines.append(
                f"| {w.mode.value.replace('_', ' ')} | {w.suggested_departure} | "
                f"{w.duration_minutes} min | {w.route_summary} |"
            )
        lines.append("")

    if d := plan.lunch:
        lines += [f"## Lunch: {d.name} ({d.kind})", f"- **Where:** {d.area}, {d.postcode}"]
        if d.distance_from_venue_km is not None:
            lines.append(f"- **Distance from venue:** {d.distance_from_venue_km:.1f} km")
        lines.append(f"- **Good picks:** {', '.join(d.menu_highlights)}")
        if d.high_protein_options:
            lines.append(f"- **High-protein:** {', '.join(d.high_protein_options)}")
        lines.append("")

    if plan.backup_venues or plan.backup_dining:
        lines.append("## Plan B")
        lines += [f"- **{b.name}** ({b.setting.value}, {b.area} {b.postcode})" for b in plan.backup_venues]
        lines += [f"- Lunch alternative: **{b.name}** ({b.area})" for b in plan.backup_dining]
        lines.append("")

    if plan.resolution_log:
        lines += ["<details><summary>Supervisor decisions</summary>", ""]
        lines += [f"- `{n.rule}`: {n.detail}" for n in plan.resolution_log]
        lines += ["", "</details>", ""]
    return "\n".join(lines)


def save_itinerary(markdown: str, constraints: TripConstraints, outputs_dir: Path = OUTPUTS_DIR) -> Path:
    slug = re.sub(r"[^a-z0-9]+", "-", constraints.origin.lower()).strip("-")[:40] or "trip"
    path = Path(outputs_dir) / f"{constraints.target_date.isoformat()}-{slug}.md"
    if not is_inside_outputs(path):  # the same guard the hooks apply to agents
        raise PermissionError(f"refusing to write outside {OUTPUTS_DIR}: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return path


# ==========================================================================
# Orchestration
# ==========================================================================


def parser_spec() -> AgentSpec:
    return AgentSpec(
        name="supervisor",
        system_prompt=PARSER_PROMPT,
        output_model=TripConstraints,
        builtin_tools=[],
        mcp_servers={"preferences": build_preferences_server()},
        mcp_tool_names=PREFS_TOOLS,
        model=SUPERVISOR_MODEL,
        max_turns=4,
        max_budget_usd=0.50,
    )


class Supervisor:
    def __init__(
        self,
        runner: AgentRunner = run_structured_agent,
        trace: TraceLogger | None = None,
        today: date | None = None,
        timeout_s: float = SUBAGENT_TIMEOUT_S,
    ) -> None:
        self.runner = runner
        self.trace = trace or TraceLogger()
        self.today = today or date.today()
        self.timeout_s = timeout_s

    async def parse_request(self, request: str) -> TripConstraints:
        prompt = f"Today is {self.today:%A %Y-%m-%d}.\n\nUser request:\n{request}"
        result = await self.runner(parser_spec(), prompt, self.trace)
        assert isinstance(result, TripConstraints)
        return result

    async def _guarded(self, spec: AgentSpec, prompt: str) -> Any:
        try:
            return await asyncio.wait_for(self.runner(spec, prompt, self.trace), self.timeout_s)
        except TimeoutError as exc:
            raise SubagentError(spec.name, f"timed out after {self.timeout_s}s") from exc

    async def fan_out(
        self, c: TripConstraints
    ) -> tuple[VenueReport | None, LogisticsReport | None, FoodReport | None]:
        """Run all three subagents concurrently. One failing doesn't cancel the others."""
        jobs = [
            (venue_agent.build_spec(), venue_agent.build_prompt(c)),
            (logistics_agent.build_spec(), logistics_agent.build_prompt(c)),
            (food_agent.build_spec(), food_agent.build_prompt(c)),
        ]
        self.trace.write("fan_out_start", agents=[s.name for s, _ in jobs])
        results = await asyncio.gather(*(self._guarded(s, p) for s, p in jobs), return_exceptions=True)
        cleaned: list[Any] = []
        for (spec, _), res in zip(jobs, results, strict=True):
            if isinstance(res, BaseException):
                self.trace.write("agent_failed", agent=spec.name, error=str(res))
                cleaned.append(None)
            else:
                cleaned.append(res)
        self.trace.write("fan_out_done", ok=[s.name for (s, _), r in zip(jobs, cleaned, strict=True) if r])
        return cleaned[0], cleaned[1], cleaned[2]

    async def plan(self, request: str) -> tuple[ItineraryPlan, Path]:
        self.trace.write("request", text=request)
        constraints = await self.parse_request(request)
        self.trace.write("constraints", data=constraints.model_dump(mode="json"))
        venues, logistics, food = await self.fan_out(constraints)
        plan = resolve_conflicts(constraints, venues, logistics, food, load_preferences())
        path = save_itinerary(render_markdown(plan), constraints)
        self.trace.write("itinerary_saved", path=str(path), warnings=plan.warnings)
        return plan, path
