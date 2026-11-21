from __future__ import annotations

import pytest

from hooks import TraceLogger

PREFS = {
    "dietary": {
        "excluded_ingredients": ["raw tomatoes", "aubergine", "courgette"],
        "prioritise": ["high-protein"],
    },
    "planning": {"rain_threshold_pct": 60, "max_lunch_distance_km": 2.0, "prefer_public_transport": False},
}


@pytest.fixture
def prefs() -> dict:
    import copy

    return copy.deepcopy(PREFS)


@pytest.fixture
def trace(tmp_path) -> TraceLogger:
    return TraceLogger(path=tmp_path / "trace.jsonl", run_id="test")
