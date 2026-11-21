"""Pydantic contracts between the Supervisor and its subagents.

Every subagent is launched with ``output_format`` set to the JSON Schema of
its report model, and its result is validated again with ``model_validate``
before the Supervisor sees it. If either step fails, the subagent fails.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    """Base model: unknown keys are rejected so schema drift fails loudly."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------
# Supervisor input: the parsed request
# --------------------------------------------------------------------------


class GroupProfile(_Strict):
    adults: int = Field(ge=0, default=2)
    children: int = Field(ge=0, default=0)
    child_ages: list[int] = Field(default_factory=list)
    accessibility_needs: list[str] = Field(
        default_factory=list, description="e.g. step-free access, pushchair-friendly"
    )


class TripConstraints(_Strict):
    origin: str = Field(description="Starting location, e.g. 'Chislehurst, BR7'")
    target_date: date
    start_time: str = Field(default="09:30", description="HH:MM, local time")
    end_time: str = Field(default="17:30", description="HH:MM, local time")
    max_travel_minutes: int = Field(ge=5, le=240, default=45)
    group: GroupProfile = Field(default_factory=GroupProfile)
    preferences: list[str] = Field(
        default_factory=list, description="Free-text wishes, e.g. 'lunch options', 'nature'"
    )
    candidate_areas: list[str] = Field(
        default_factory=list,
        description="Towns/areas within the travel radius that subagents should search",
    )


# --------------------------------------------------------------------------
# Venue & Activity Scout
# --------------------------------------------------------------------------


class Setting(StrEnum):
    INDOOR = "indoor"
    OUTDOOR = "outdoor"
    MIXED = "mixed"


class Venue(_Strict):
    name: str
    postcode: str
    area: str = Field(description="Town/area the venue is in")
    setting: Setting
    estimated_duration_minutes: int = Field(ge=15, le=600)
    booking_required: bool
    booking_notes: str = ""
    suitability_notes: str = Field(default="", description="Why it fits the group")
    approx_cost_gbp: float | None = Field(default=None, ge=0)
    url: str | None = None


class VenueReport(_Strict):
    venues: list[Venue] = Field(min_length=1)
    search_notes: str = ""


# --------------------------------------------------------------------------
# Weather & Travel Analyst
# --------------------------------------------------------------------------


class WeatherRisk(StrEnum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class WeatherForecast(_Strict):
    summary: str
    max_temp_c: float
    min_temp_c: float
    precipitation_probability_pct: int = Field(ge=0, le=100)
    wind_speed_kmh: float = Field(ge=0)
    risk_level: WeatherRisk
    risk_reasons: list[str] = Field(default_factory=list)


class TransportMode(StrEnum):
    DRIVING = "driving"
    PUBLIC_TRANSPORT = "public_transport"


class TransitWindow(_Strict):
    destination_area: str
    mode: TransportMode
    duration_minutes: int = Field(ge=0)
    suggested_departure: str = Field(description="HH:MM")
    route_summary: str
    notes: str = ""


class LogisticsReport(_Strict):
    forecast: WeatherForecast
    transit_windows: list[TransitWindow] = Field(min_length=1)
    data_sources: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Dining & Dietary Filter
# --------------------------------------------------------------------------


class DiningOption(_Strict):
    name: str
    kind: str = Field(description="pub | cafe | restaurant | other")
    area: str
    postcode: str
    menu_highlights: list[str] = Field(min_length=1)
    high_protein_options: list[str] = Field(default_factory=list)
    flagged_ingredients: list[str] = Field(
        default_factory=list, description="Excluded ingredients seen on the menu"
    )
    family_friendly: bool = True
    distance_from_venue_km: float | None = Field(default=None, ge=0)
    nearest_venue: str | None = None
    booking_recommended: bool = False


class FoodReport(_Strict):
    options: list[DiningOption] = Field(min_length=1)
    exclusions_applied: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Supervisor output
# --------------------------------------------------------------------------


class ResolutionNote(_Strict):
    rule: str
    detail: str


class ItineraryPlan(_Strict):
    constraints: TripConstraints
    primary_venue: Venue | None
    backup_venues: list[Venue] = Field(default_factory=list)
    lunch: DiningOption | None
    backup_dining: list[DiningOption] = Field(default_factory=list)
    travel: list[TransitWindow] = Field(default_factory=list)
    forecast: WeatherForecast | None
    resolution_log: list[ResolutionNote] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
