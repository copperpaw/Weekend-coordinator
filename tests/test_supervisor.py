"""Supervisor tests: conflict resolution, parallel fan-out, failure handling.

No network and no LLM: subagent outputs come from ``schemas/examples.py``
and a fake ``AgentRunner`` replaces the SDK call.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

import agents.supervisor as sup
from agents import food_agent, logistics_agent, venue_agent
from agents.base import SubagentError, build_options, validate_output
from schemas import examples as ex
from schemas.models import FoodReport, Setting, TripConstraints, VenueReport

# ---------------------------------------------------------------- resolution


def rules(plan) -> list[str]:
    return [n.rule for n in plan.resolution_log]


def test_dry_weather_keeps_scout_order_and_pairs_nearby_lunch(prefs):
    plan = sup.resolve_conflicts(ex.CONSTRAINTS, ex.VENUES, ex.LOGISTICS_DRY, ex.FOOD, prefs)

    assert plan.primary_venue.name == "Knole Park"  # outdoor is fine when dry
    assert plan.lunch.name == "The Old House at Home"  # nearest_venue match
    assert "weather.indoor_priority" not in rules(plan)
    assert [w.destination_area for w in plan.travel] == ["Sevenoaks"]


def test_wet_weather_prioritises_indoor_venue(prefs):
    plan = sup.resolve_conflicts(ex.CONSTRAINTS, ex.VENUES, ex.LOGISTICS_WET, ex.FOOD, prefs)

    assert plan.primary_venue.name == "National Maritime Museum"
    assert plan.primary_venue.setting is Setting.INDOOR
    assert "weather.indoor_priority" in rules(plan)
    # outdoor venues are not deleted; they become (lower-ranked) backups
    assert plan.backup_venues[0].setting is not Setting.OUTDOOR
    assert {v.name for v in plan.backup_venues} >= {"Cutty Sark"}


def test_rain_threshold_from_preferences_triggers_indoor_even_if_risk_low(prefs):
    prefs["planning"]["rain_threshold_pct"] = 5  # 10% rain now counts as "bad"
    plan = sup.resolve_conflicts(ex.CONSTRAINTS, ex.VENUES, ex.LOGISTICS_DRY, ex.FOOD, prefs)
    assert plan.primary_venue.setting is Setting.INDOOR


def test_wet_lunch_follows_venue_and_strips_excluded_highlights(prefs):
    plan = sup.resolve_conflicts(ex.CONSTRAINTS, ex.VENUES, ex.LOGISTICS_WET, ex.FOOD, prefs)

    assert plan.lunch.name == "Goddards Pie & Mash"
    assert plan.lunch.menu_highlights == ["Pie and mash"]  # courgette dish removed
    assert "diet.highlight_removed" in rules(plan)


def test_option_with_only_excluded_dishes_is_dropped(prefs):
    food = FoodReport.model_validate(
        {
            "options": [
                {
                    "name": "Veg Shack",
                    "kind": "cafe",
                    "area": "Greenwich",
                    "postcode": "SE10",
                    "menu_highlights": ["Grilled aubergines", "Courgette fritters"],
                },
                {
                    "name": "Grill House",
                    "kind": "restaurant",
                    "area": "Greenwich",
                    "postcode": "SE10",
                    "menu_highlights": ["Chicken skewers"],
                    "high_protein_options": ["Chicken skewers"],
                },
            ]
        }
    )
    plan = sup.resolve_conflicts(ex.CONSTRAINTS, ex.VENUES, ex.LOGISTICS_WET, food, prefs)
    assert plan.lunch.name == "Grill House"
    assert "diet.option_dropped" in rules(plan)
    assert all(o.name != "Veg Shack" for o in plan.backup_dining)


def test_flagged_ingredients_down_rank_and_warn(prefs):
    # Only the flagged option is in Greenwich besides Goddards; Goddards should win.
    plan = sup.resolve_conflicts(ex.CONSTRAINTS, ex.VENUES, ex.LOGISTICS_WET, ex.FOOD, prefs)
    assert plan.lunch.name != "Greenwich Market Grill"


def test_venue_beyond_travel_radius_is_dropped(prefs):
    tight = ex.CONSTRAINTS.model_copy(update={"max_travel_minutes": 28})
    plan = sup.resolve_conflicts(tight, ex.VENUES, ex.LOGISTICS_DRY, ex.FOOD, prefs)

    names = {plan.primary_venue.name, *(v.name for v in plan.backup_venues)}
    assert "Crystal Palace Park Dinosaurs" not in names  # 30 min > 28
    assert "National Maritime Museum" not in names  # best Greenwich option is 30 min
    assert plan.primary_venue.name == "Knole Park"  # 25 min
    assert "travel.radius_exceeded" in rules(plan)


def test_venue_too_long_for_day_moves_to_backup(prefs):
    short_day = ex.CONSTRAINTS.model_copy(update={"start_time": "11:00", "end_time": "15:00"})
    plan = sup.resolve_conflicts(short_day, ex.VENUES, ex.LOGISTICS_WET, ex.FOOD, prefs)
    # Maritime Museum: 180 + 2*30 + 60 > 240, so the short Cutty Sark visit wins
    assert plan.primary_venue.name == "Cutty Sark"
    assert "schedule.too_long" in rules(plan)
    assert any("Book Cutty Sark" in w for w in plan.warnings)


def test_prefer_public_transport_uses_train_time(prefs):
    prefs["planning"]["prefer_public_transport"] = True
    window = sup.best_transit("Greenwich", ex.LOGISTICS_DRY.transit_windows, prefer_public=True)
    assert window.mode.value == "public_transport" and window.duration_minutes == 35


def test_missing_reports_degrade_gracefully(prefs):
    plan = sup.resolve_conflicts(ex.CONSTRAINTS, ex.VENUES, None, None, prefs)
    assert plan.primary_venue is not None
    assert plan.lunch is None and plan.forecast is None
    assert any("Logistics agent failed" in w for w in plan.warnings)
    assert any("Food agent failed" in w for w in plan.warnings)


def test_resolution_is_deterministic(prefs):
    a = sup.resolve_conflicts(ex.CONSTRAINTS, ex.VENUES, ex.LOGISTICS_WET, ex.FOOD, prefs)
    b = sup.resolve_conflicts(ex.CONSTRAINTS, ex.VENUES, ex.LOGISTICS_WET, ex.FOOD, prefs)
    assert a.model_dump() == b.model_dump()


@pytest.mark.parametrize(
    ("text", "hits"),
    [
        ("Tomato & mozzarella salad with raw tomatoes", ["raw tomatoes"]),
        ("Slow-cooked tomato ragu", []),  # cooked tomato is fine
        ("Grilled Aubergines", ["aubergine"]),
        ("Courgettes and feta", ["courgette"]),
    ],
)
def test_mentions_excluded(text, hits):
    assert sup.mentions_excluded(text, ["raw tomatoes", "aubergine", "courgette"]) == hits


# ----------------------------------------------------------------- rendering


def test_render_markdown_contains_key_sections(prefs):
    plan = sup.resolve_conflicts(ex.CONSTRAINTS, ex.VENUES, ex.LOGISTICS_WET, ex.FOOD, prefs)
    md = sup.render_markdown(plan)
    for heading in (
        "# Day Trip: Saturday 28 November 2026",
        "## Weather",
        "## Main activity",
        "## Getting there",
        "## Lunch",
        "## Plan B",
        "Supervisor decisions",
    ):
        assert heading in md
    picks = next(line for line in md.splitlines() if line.startswith("- **Good picks:**"))
    assert "Ratatouille" not in picks  # removed from the menu, explained in the decision log
    assert "Ratatouille" in md.split("Supervisor decisions")[1]


def test_save_itinerary_refuses_paths_outside_outputs(tmp_path):
    with pytest.raises(PermissionError):
        sup.save_itinerary("x", ex.CONSTRAINTS, outputs_dir=tmp_path)


# ----------------------------------------------------------------- fan-out


def fake_runner(delay: float = 0.2, fail: set[str] | None = None, hang: set[str] | None = None):
    fixtures = {
        "supervisor": ex.CONSTRAINTS,
        "venue_agent": ex.VENUES,
        "logistics_agent": ex.LOGISTICS_WET,
        "food_agent": ex.FOOD,
    }
    calls: list[str] = []

    async def run(spec, prompt, trace):
        calls.append(spec.name)
        if hang and spec.name in hang:
            await asyncio.sleep(60)
        await asyncio.sleep(delay)
        if fail and spec.name in fail:
            raise SubagentError(spec.name, "boom")
        return fixtures[spec.name]

    run.calls = calls  # type: ignore[attr-defined]
    return run


async def test_fan_out_runs_subagents_in_parallel(trace):
    s = sup.Supervisor(runner=fake_runner(delay=0.3), trace=trace)
    t0 = time.perf_counter()
    venues, logistics, food = await s.fan_out(ex.CONSTRAINTS)
    elapsed = time.perf_counter() - t0
    assert elapsed < 0.6, f"subagents ran sequentially ({elapsed:.2f}s)"
    assert (venues, logistics, food) == (ex.VENUES, ex.LOGISTICS_WET, ex.FOOD)


async def test_one_failing_subagent_does_not_cancel_others(trace):
    s = sup.Supervisor(runner=fake_runner(fail={"food_agent"}), trace=trace)
    venues, logistics, food = await s.fan_out(ex.CONSTRAINTS)
    assert venues is not None and logistics is not None and food is None
    events = [json.loads(line) for line in trace.path.read_text().splitlines()]
    assert any(e["event"] == "agent_failed" and e["agent"] == "food_agent" for e in events)


async def test_hung_subagent_times_out(trace):
    s = sup.Supervisor(runner=fake_runner(delay=0, hang={"venue_agent"}), trace=trace, timeout_s=0.2)
    venues, logistics, _ = await s.fan_out(ex.CONSTRAINTS)
    assert venues is None and logistics is not None


async def test_end_to_end_plan_with_fake_runner(trace, tmp_path, monkeypatch, prefs):
    saved = {}

    def fake_save(md, constraints):
        saved["md"] = md
        path = tmp_path / "plan.md"
        path.write_text(md)
        return path

    monkeypatch.setattr(sup, "save_itinerary", fake_save)
    monkeypatch.setattr(sup, "load_preferences", lambda: prefs)
    runner = fake_runner(delay=0)
    plan, path = await sup.Supervisor(runner=runner, trace=trace).plan("rainy saturday please")

    assert runner.calls[0] == "supervisor"  # parse first, then fan out
    assert sorted(runner.calls[1:]) == ["food_agent", "logistics_agent", "venue_agent"]
    assert plan.primary_venue.setting is Setting.INDOOR
    assert "National Maritime Museum" in saved["md"]


# --------------------------------------------- isolation & guardrail config


def test_subagents_have_isolated_tool_surfaces():
    v, lg, f = venue_agent.build_spec(), logistics_agent.build_spec(), food_agent.build_spec()
    assert set(v.mcp_servers) == set()
    assert set(lg.mcp_servers) == {"travel"}
    assert set(f.mcp_servers) == {"preferences"}
    for spec in (v, lg, f):
        assert not {"Write", "Edit", "Bash"} & set(spec.builtin_tools)


def test_build_options_applies_guardrails(trace):
    spec = venue_agent.build_spec()
    opts = build_options(spec, trace)
    assert opts.max_turns == spec.max_turns and opts.max_turns <= 20
    assert opts.max_budget_usd == spec.max_budget_usd
    assert opts.permission_mode == "dontAsk"
    assert opts.setting_sources == []
    assert opts.tools == ["WebSearch", "WebFetch"]
    assert opts.output_format["schema"] == VenueReport.model_json_schema()
    assert set(opts.hooks) == {"PreToolUse", "PostToolUse"}


def test_validate_output_accepts_fenced_json_fallback():
    text = "Here you go:\n```json\n" + ex.CONSTRAINTS.model_dump_json() + "\n```"
    assert validate_output("supervisor", TripConstraints, text) == ex.CONSTRAINTS


def test_validate_output_rejects_schema_drift():
    bad = ex.VENUES.model_dump(mode="json")
    bad["venues"][0]["setting"] = "underwater"
    with pytest.raises(SubagentError, match="VenueReport validation"):
        validate_output("venue_agent", VenueReport, bad)
