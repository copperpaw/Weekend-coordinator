"""Realistic sample payloads matching each subagent contract.

Used by the pytest suite and by ``python main.py --demo``, which runs the
whole pipeline offline with a fake runner. The details are illustrative
only; check them before relying on them.
"""

from __future__ import annotations

from datetime import date

from schemas.models import (
    FoodReport,
    GroupProfile,
    LogisticsReport,
    TripConstraints,
    VenueReport,
)

CONSTRAINTS = TripConstraints(
    origin="Chislehurst, BR7",
    target_date=date(2026, 11, 28),
    start_time="09:30",
    end_time="17:30",
    max_travel_minutes=45,
    group=GroupProfile(adults=2, children=2, child_ages=[3, 6]),
    preferences=["family-friendly", "lunch options"],
    candidate_areas=["Greenwich", "Sevenoaks", "Bromley", "Crystal Palace"],
)

VENUES = VenueReport.model_validate(
    {
        "venues": [
            {
                "name": "Knole Park",
                "postcode": "TN15 0RP",
                "area": "Sevenoaks",
                "setting": "outdoor",
                "estimated_duration_minutes": 150,
                "booking_required": False,
                "suitability_notes": "Deer park with wide paths, pushchair-friendly.",
            },
            {
                "name": "National Maritime Museum",
                "postcode": "SE10 9NF",
                "area": "Greenwich",
                "setting": "indoor",
                "estimated_duration_minutes": 180,
                "booking_required": False,
                "suitability_notes": "Free entry; AHOY! children's gallery for under-7s.",
                "approx_cost_gbp": 0,
            },
            {
                "name": "Crystal Palace Park Dinosaurs",
                "postcode": "SE20 8DT",
                "area": "Crystal Palace",
                "setting": "outdoor",
                "estimated_duration_minutes": 120,
                "booking_required": False,
            },
            {
                "name": "Cutty Sark",
                "postcode": "SE10 9HT",
                "area": "Greenwich",
                "setting": "mixed",
                "estimated_duration_minutes": 90,
                "booking_required": True,
                "booking_notes": "Timed tickets; book online.",
                "approx_cost_gbp": 48,
            },
        ],
        "search_notes": "Sample data.",
    }
)

LOGISTICS_DRY = LogisticsReport.model_validate(
    {
        "forecast": {
            "summary": "Bright and dry",
            "max_temp_c": 11,
            "min_temp_c": 4,
            "precipitation_probability_pct": 10,
            "wind_speed_kmh": 12,
            "risk_level": "low",
        },
        "transit_windows": [
            {
                "destination_area": "Sevenoaks",
                "mode": "driving",
                "duration_minutes": 25,
                "suggested_departure": "09:30",
                "route_summary": "A21 south",
            },
            {
                "destination_area": "Greenwich",
                "mode": "public_transport",
                "duration_minutes": 35,
                "suggested_departure": "09:40",
                "route_summary": "Southeastern to Lewisham, DLR",
            },
            {
                "destination_area": "Greenwich",
                "mode": "driving",
                "duration_minutes": 30,
                "suggested_departure": "09:35",
                "route_summary": "A20 / A2",
            },
            {
                "destination_area": "Crystal Palace",
                "mode": "driving",
                "duration_minutes": 30,
                "suggested_departure": "09:35",
                "route_summary": "A222",
            },
        ],
        "data_sources": ["sample"],
    }
)

# model_validate, not model_copy(update=...), which would skip validation.
LOGISTICS_WET = LogisticsReport.model_validate(
    {
        **LOGISTICS_DRY.model_dump(mode="json"),
        "forecast": {
            **LOGISTICS_DRY.forecast.model_dump(mode="json"),
            "summary": "Persistent rain",
            "precipitation_probability_pct": 85,
            "risk_level": "high",
            "risk_reasons": ["rain probability 85%"],
        },
    }
)

FOOD = FoodReport.model_validate(
    {
        "options": [
            {
                "name": "The Old House at Home",
                "kind": "pub",
                "area": "Sevenoaks",
                "postcode": "TN13 1AA",
                "menu_highlights": ["Steak and ale pie", "Roast chicken"],
                "high_protein_options": ["Steak and ale pie", "Roast chicken"],
                "nearest_venue": "Knole Park",
                "distance_from_venue_km": 1.2,
            },
            {
                "name": "Goddards Pie & Mash",
                "kind": "cafe",
                "area": "Greenwich",
                "postcode": "SE10 9HZ",
                "menu_highlights": ["Pie and mash", "Ratatouille with courgettes"],
                "high_protein_options": ["Pie and mash"],
                "nearest_venue": "National Maritime Museum",
                "distance_from_venue_km": 0.4,
            },
            {
                "name": "Greenwich Market Grill",
                "kind": "restaurant",
                "area": "Greenwich",
                "postcode": "SE10 9HZ",
                "menu_highlights": ["Halloumi burger"],
                "flagged_ingredients": ["aubergine"],
                "distance_from_venue_km": 0.5,
            },
        ],
        "exclusions_applied": ["raw tomatoes", "aubergine", "courgette"],
    }
)
