from __future__ import annotations

import csv
import math
import re
from functools import lru_cache
from importlib import resources

EARTH_RADIUS_MI = 3958.8

# The Census "internal point" for San Francisco sits offshore because the city
# limits include the Farallon Islands.
OVERRIDES = {("san francisco", "CA"): (37.7749, -122.4194)}


def haversine_mi(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MI * math.asin(math.sqrt(h))


@lru_cache(maxsize=1)
def _places() -> dict[tuple[str, str], tuple[float, float]]:
    """Every US city, town and census-designated place (see scripts/build_places.py)."""
    places: dict[tuple[str, str], tuple[float, float]] = {}
    path = resources.files("marketplace_finder") / "data" / "places_us.csv"
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            places[(row["name"].lower(), row["state"])] = (float(row["lat"]), float(row["lon"]))
    places.update(OVERRIDES)
    return places


def lookup_city(text: str) -> tuple[float, float] | None:
    """Coordinates for a "City, ST" string, else None."""
    match = re.match(r"\s*([^,]+?)\s*,\s*([A-Za-z]{2})\b", text or "")
    if not match:
        return None
    return _places().get((match.group(1).lower(), match.group(2).upper()))


def state_of(text: str) -> str | None:
    match = re.match(r"\s*[^,]+?\s*,\s*([A-Za-z]{2})\b", text or "")
    return match.group(1).upper() if match else None


@lru_cache(maxsize=8)
def nearby_states(home: tuple[float, float], miles: float) -> frozenset[str]:
    """The states with at least one place within `miles` of `home`."""
    return frozenset(
        state for (_, state), coords in _places().items() if haversine_mi(home, coords) <= miles
    )
