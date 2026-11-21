from __future__ import annotations

import json

import pytest

import tools.preferences_tool as pt
from tools.travel_tools import summarise_forecast


def test_apply_update_add_is_case_insensitive_and_idempotent(prefs):
    out = pt.apply_update(prefs, "excluded_ingredients", "add", "Mushrooms")
    out = pt.apply_update(out, "excluded_ingredients", "add", "mushrooms")
    assert out["dietary"]["excluded_ingredients"].count("Mushrooms") == 1
    assert "Mushrooms" not in prefs["dietary"]["excluded_ingredients"]  # input not mutated


def test_apply_update_remove_and_set(prefs):
    out = pt.apply_update(prefs, "excluded_ingredients", "remove", "AUBERGINE")
    assert "aubergine" not in out["dietary"]["excluded_ingredients"]
    out = pt.apply_update(out, "rain_threshold_pct", "set", "40")
    assert out["planning"]["rain_threshold_pct"] == 40
    out = pt.apply_update(out, "prefer_public_transport", "set", "yes")
    assert out["planning"]["prefer_public_transport"] is True


def test_apply_update_rejects_unknown(prefs):
    with pytest.raises(ValueError):
        pt.apply_update(prefs, "favourite_colour", "set", "blue")


async def test_mcp_tools_round_trip(tmp_path, monkeypatch, prefs):
    path = tmp_path / "prefs.yaml"
    pt.save_preferences(prefs, path)
    monkeypatch.setattr(pt, "PREFS_PATH", path)

    res = await pt.update_preferences.handler(
        {"field": "excluded_ingredients", "action": "add", "value": "olives"}
    )
    assert json.loads(res["content"][0]["text"])["status"] == "saved"

    res = await pt.get_preferences.handler({})
    assert "olives" in json.loads(res["content"][0]["text"])["dietary"]["excluded_ingredients"]

    res = await pt.update_preferences.handler({"field": "nope", "action": "set", "value": "x"})
    assert res.get("is_error") is True


def test_shipped_preferences_file_is_valid():
    prefs = pt.load_preferences()
    assert {"raw tomatoes", "aubergine", "courgette"} <= set(prefs["dietary"]["excluded_ingredients"])


def test_summarise_forecast_picks_target_day():
    daily = {
        "time": ["2026-11-27", "2026-11-28"],
        "weather_code": [3, 61],
        "temperature_2m_max": [10, 9],
        "temperature_2m_min": [3, 4],
        "precipitation_probability_max": [20, 80],
        "precipitation_sum": [0, 6.2],
        "wind_speed_10m_max": [15, 30],
    }
    s = summarise_forecast(daily, "2026-11-28")
    assert s["precipitation_probability_pct"] == 80 and s["weather_code"] == 61
    assert summarise_forecast(daily, "2026-12-25") is None
