# Weekend & Logistics Coordinator

A CLI multi-agent day-trip planner built on the **Claude Agent SDK** (`claude-agent-sdk`, Python).
It's a portfolio project demonstrating enterprise agent patterns: **Supervisor + parallel fan-out**,
**isolated subagent contexts**, **Pydantic-validated structured outputs**, **deterministic safety
hooks**, and **custom in-process MCP tools**.

## Commands

```bash
uv sync                                   # install (Python >= 3.11)
cp .env.example .env                      # then set ANTHROPIC_API_KEY
uv run python main.py "Plan a family-friendly Saturday day trip within 45 minutes of Chislehurst, with lunch"
uv run python main.py --demo              # offline: sample subagent outputs, no API key or network
uv run python main.py --demo --wet        # offline: rainy forecast, shows indoor-priority resolution
uv run pytest                             # all tests are offline and mocked (~1s)
uv run ruff check . && uv run ruff format .
```

Outputs: itinerary → `outputs/<date>-<origin-slug>.md`; audit trail → `logs/agent_trace.jsonl`.

## Architecture

```
main.py ─► orchestrator.py (facade) ─► agents/supervisor.py :: Supervisor.plan()
  1. parse_request   LLM (WC_SUPERVISOR_MODEL, default claude-opus-5-5) → TripConstraints
                     has the preferences MCP tools (it can update preferences on request)
  2. fan_out         asyncio.gather(venue, logistics, food), return_exceptions=True,
                     each wrapped in asyncio.wait_for(WC_SUBAGENT_TIMEOUT_S)
                     subagents use WC_SUBAGENT_MODEL (default claude-sonnet-5-5)
  3. resolve_conflicts  PURE PYTHON: deterministic, logged in ItineraryPlan.resolution_log
  4. render_markdown + save_itinerary → outputs/
```

### Isolation and guardrails (`agents/base.py :: build_options`)
Each agent run is a separate `query()` call, so it gets its own Claude Code subprocess and its own
context window. Every run sets:
- `max_turns` (the "max_steps" limit; `WC_SUBAGENT_MAX_TURNS`, default 12; parser: 4)
- `max_budget_usd` (`WC_SUBAGENT_BUDGET_USD`, default 0.75)
- `tools=[...]` explicit built-in allowlist + `permission_mode="dontAsk"` (anything else is refused)
- `setting_sources=[]` so no user/project settings or this CLAUDE.md leak into subagent context
- `hooks=trace.hooks_for(agent_name)` (see below)
- `output_format={"type": "json_schema", "schema": Model.model_json_schema()}`; the result is
  then **re-validated** with `Model.model_validate` (`validate_output`). Falls back to parsing
  fenced/inline JSON from `result` if `structured_output` is empty.

`AgentRunner = Callable[[AgentSpec, str, TraceLogger], Awaitable[BaseModel]]` is the seam for
testing: `Supervisor(runner=fake)` swaps out the SDK entirely (tests and `--demo` both use it).

### Subagents (`agents/`)
| Module | Output model | Tools | Notes |
|---|---|---|---|
| `venue_agent.py` | `VenueReport` | WebSearch, WebFetch | must return both indoor and outdoor options |
| `logistics_agent.py` | `LogisticsReport` | WebSearch + `travel` MCP (geocode, weather_forecast, public_transport_journey) | Open-Meteo + TfL, no keys; driving times via WebSearch |
| `food_agent.py` | `FoodReport` | WebSearch, WebFetch + `preferences` MCP | must call `get_preferences` first |

Each module exposes `SYSTEM_PROMPT`, `build_spec() -> AgentSpec` and
`build_prompt(TripConstraints) -> str`. Subagents never get Write, Edit or Bash.

### Schemas (`schemas/models.py`)
Every model extends `_Strict` (`extra="forbid"`), so schema drift fails validation.
- `TripConstraints` (origin, target_date, start/end_time, max_travel_minutes, `GroupProfile`, preferences, candidate_areas)
- `VenueReport.venues[]: Venue` (name, postcode, area, setting=indoor|outdoor|mixed, estimated_duration_minutes, booking_required, booking_notes, …)
- `LogisticsReport` (forecast: `WeatherForecast` with `risk_level` low|moderate|high; transit_windows[]: `TransitWindow` with mode driving|public_transport, duration_minutes, suggested_departure)
- `FoodReport.options[]: DiningOption` (menu_highlights, high_protein_options, flagged_ingredients, distance_from_venue_km, nearest_venue)
- `ItineraryPlan` (the Supervisor's output, including `resolution_log` and `warnings`)

`schemas/examples.py` holds valid sample instances used by the tests and `--demo`.

### Conflict-resolution rules (`resolve_conflicts`)
Each rule appends a `ResolutionNote(rule=...)`:
1. `weather.indoor_priority`: risk high **or** rain% ≥ `planning.rain_threshold_pct` → rank indoor > mixed > outdoor; outdoor becomes Plan B. `weather.mixed_priority` for moderate risk.
2. `travel.radius_exceeded`: a venue whose best transit window (public transport if `prefer_public_transport`) exceeds `max_travel_minutes` is dropped.
3. `schedule.too_long`: visit + 2×travel + 60 min lunch > day window → down-ranked to backup.
4. `diet.highlight_removed` / `diet.option_dropped`: excluded ingredients are re-checked in code (plural-insensitive phrase match, so "raw tomatoes" doesn't match "tomato ragu").
5. `diet.lunch_pairing`: dining ranked by nearest_venue/area match → within `max_lunch_distance_km` → fewer flagged ingredients → more high-protein options.
Ties keep the subagent's own order. Missing reports (failed agents) produce warnings, not crashes.

### Hooks (`hooks.py`)
`TraceLogger.hooks_for(agent)` returns `PreToolUse` + `PostToolUse` matchers:
- **PreToolUse** logs the call and returns `permissionDecision: "deny"` for Write, Edit,
  MultiEdit or NotebookEdit targeting a path outside `outputs/` (resolved, so `..` and symlinks are
  caught), and for Bash commands whose redirects or `tee/cp/mv/…` targets fall outside `outputs/`.
- **PostToolUse** logs `duration_ms` (from the CLI, or measured from the Pre timestamp).
- Token usage and cost come from `ResultMessage` via `TraceLogger.log_usage` (tool hooks don't
  carry tokens). All of this goes to the same JSONL file with a shared `run_id`.
- `save_itinerary` applies the same `is_inside_outputs` check to the Supervisor's own write.

### Custom tools (`tools/`)
- `preferences_tool.py`: `create_sdk_mcp_server("preferences")` with `get_preferences` and
  `update_preferences` (add/remove on `excluded_ingredients`/`prioritise`, set on planning keys).
  Pure helpers `load_preferences`, `apply_update` and `save_preferences` (atomic write) are tested directly.
  Claude sees these tools as `mcp__preferences__<tool>`.
- `travel_tools.py`: `create_sdk_mcp_server("travel")`. HTTP errors come back as `is_error`
  results, never exceptions. `summarise_forecast` is pure and tested.

## Conventions
- Python ≥ 3.11, `from __future__ import annotations`, type hints everywhere, ruff (line length 110).
- **Decisions belong in Python, research belongs in the LLM.** Put new ranking or filtering logic in
  `resolve_conflicts` with a `ResolutionNote` and a unit test. Don't add it to a prompt.
- New subagent: add a model in `schemas/models.py` plus a sample in `examples.py`, a module in `agents/`
  with `build_spec`/`build_prompt`, a job in `Supervisor.fan_out`, a fixture in `main.demo_runner`, and tests.
- New `@tool`: return `{"content": [{"type": "text", "text": ...}]}`, add `"is_error": True` on
  failure, export `TOOL_NAMES` (`mcp__<server>__<tool>`) and add them to the spec's `mcp_tool_names`.
- Tests must stay offline: use a fake `AgentRunner`, and `tmp_path` + `monkeypatch` for files.
- Build pydantic fixtures with `model_validate`, never `model_copy(update=...)` (which skips validation).
- Never write outside `outputs/` (enforced by hooks) and never commit `.env`.

## Environment variables
See `.env.example`. Required for live runs: `ANTHROPIC_API_KEY`. Optional: `WC_SUPERVISOR_MODEL`,
`WC_SUBAGENT_MODEL`, `WC_SUBAGENT_MAX_TURNS`, `WC_SUBAGENT_BUDGET_USD`, `WC_SUBAGENT_TIMEOUT_S`,
`TFL_APP_KEY`, `WC_PREFERENCES_PATH`, `WC_OUTPUTS_DIR`, `WC_TRACE_PATH`.
