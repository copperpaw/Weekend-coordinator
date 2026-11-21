"""In-process MCP server wrapping free, keyless travel and weather APIs.

* Open-Meteo geocoding + daily forecast (https://open-meteo.com)
* TfL Journey Planner for public transport (https://api.tfl.gov.uk)

Only the Logistics Agent gets these tools. They return compact JSON so the
subagent's context window stays small. Driving times are estimated by the
agent via WebSearch (there's no free, keyless driving-directions API).
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.parse import quote

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool

SERVER_NAME = "travel"
TOOL_NAMES = [
    f"mcp__{SERVER_NAME}__geocode",
    f"mcp__{SERVER_NAME}__weather_forecast",
    f"mcp__{SERVER_NAME}__public_transport_journey",
]
TIMEOUT = httpx.Timeout(15.0)
TFL_APP_KEY = os.getenv("TFL_APP_KEY")  # optional; raises TfL rate limits


def _ok(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def _err(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


async def _get(url: str, params: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        return resp.json()


@tool(
    "geocode",
    "Resolve a UK place name or postcode to latitude/longitude.",
    {"type": "object", "properties": {"place": {"type": "string"}}, "required": ["place"]},
)
async def geocode(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await _get(
            "https://geocoding-api.open-meteo.com/v1/search",
            {"name": args["place"], "count": 3, "countryCode": "GB", "format": "json"},
        )
    except httpx.HTTPError as exc:
        return _err(f"geocode failed: {exc}")
    results = [
        {k: r.get(k) for k in ("name", "latitude", "longitude", "admin1", "admin2")}
        for r in data.get("results", [])
    ]
    return _ok(results) if results else _err(f"no match for {args['place']!r}")


def summarise_forecast(daily: dict[str, list[Any]], target_date: str) -> dict[str, Any] | None:
    """Pick ``target_date`` out of an Open-Meteo ``daily`` block. Pure, so it's unit-testable."""
    dates = daily.get("time", [])
    if target_date not in dates:
        return None
    i = dates.index(target_date)
    return {
        "date": target_date,
        "weather_code": daily["weather_code"][i],
        "max_temp_c": daily["temperature_2m_max"][i],
        "min_temp_c": daily["temperature_2m_min"][i],
        "precipitation_probability_pct": daily["precipitation_probability_max"][i],
        "precipitation_mm": daily["precipitation_sum"][i],
        "wind_speed_kmh": daily["wind_speed_10m_max"][i],
    }


@tool(
    "weather_forecast",
    "Daily forecast for a lat/lon on a date (YYYY-MM-DD, up to 16 days ahead).",
    {
        "type": "object",
        "properties": {
            "latitude": {"type": "number"},
            "longitude": {"type": "number"},
            "date": {"type": "string"},
        },
        "required": ["latitude", "longitude", "date"],
    },
)
async def weather_forecast(args: dict[str, Any]) -> dict[str, Any]:
    try:
        data = await _get(
            "https://api.open-meteo.com/v1/forecast",
            {
                "latitude": args["latitude"],
                "longitude": args["longitude"],
                "daily": ",".join(
                    [
                        "weather_code",
                        "temperature_2m_max",
                        "temperature_2m_min",
                        "precipitation_probability_max",
                        "precipitation_sum",
                        "wind_speed_10m_max",
                    ]
                ),
                "timezone": "Europe/London",
                "forecast_days": 16,
            },
        )
    except httpx.HTTPError as exc:
        return _err(f"forecast failed: {exc}")
    summary = summarise_forecast(data.get("daily", {}), args["date"])
    return _ok(summary) if summary else _err(f"{args['date']} is outside the forecast window")


@tool(
    "public_transport_journey",
    "TfL journey planner. 'origin'/'destination' accept UK postcodes or 'lat,lon'. "
    "'date' is YYYYMMDD, 'time' is HHMM (departure).",
    {
        "type": "object",
        "properties": {
            "origin": {"type": "string"},
            "destination": {"type": "string"},
            "date": {"type": "string"},
            "time": {"type": "string"},
        },
        "required": ["origin", "destination", "date", "time"],
    },
)
async def public_transport_journey(args: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {"date": args["date"], "time": args["time"], "timeIs": "Departing"}
    if TFL_APP_KEY:
        params["app_key"] = TFL_APP_KEY
    url = (
        "https://api.tfl.gov.uk/Journey/JourneyResults/"
        f"{quote(args['origin'], safe=',')}/to/{quote(args['destination'], safe=',')}"
    )
    try:
        data = await _get(url, params)
    except httpx.HTTPError as exc:
        return _err(f"TfL journey lookup failed: {exc}")
    journeys = []
    for j in data.get("journeys", [])[:3]:
        journeys.append(
            {
                "duration_minutes": j.get("duration"),
                "start": j.get("startDateTime"),
                "arrive": j.get("arrivalDateTime"),
                "legs": [
                    f"{leg.get('mode', {}).get('name')}: {leg.get('instruction', {}).get('summary')}"
                    for leg in j.get("legs", [])
                ],
            }
        )
    return _ok(journeys) if journeys else _err("TfL returned no journeys (try postcodes or lat,lon)")


def build_travel_server():
    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[geocode, weather_forecast, public_transport_journey],
    )
