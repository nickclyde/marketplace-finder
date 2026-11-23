"""Deterministic checks that run before a listing is sent to Claude.

Price, distance and pickup are the same for every search. Whether the text
reads like a wanted item at all is the profile's call (`Profile.relevant`).
"""

from __future__ import annotations

from .config import Config
from .geo import haversine_mi, lookup_city, nearby_states, state_of
from .models import Listing

# City-centre lookups are approximate, so they get some slack before a listing is dropped.
APPROX_SLACK_MI = 10


def fill_distance(listing: Listing, config: Config) -> None:
    """Set distance_mi from coordinates, or from the city name when there are none."""
    if listing.lat is not None and listing.lon is not None:
        listing.distance_mi = round(haversine_mi(config.home, (listing.lat, listing.lon)), 1)
        listing.extra.pop("distance_approx", None)
    elif listing.distance_mi is None:
        coords = lookup_city(listing.location_text)
        if coords:
            listing.distance_mi = round(haversine_mi(config.home, coords), 1)
            listing.extra["distance_approx"] = True


def check(
    listing: Listing, config: Config, *, keywords: bool, strict: bool = False
) -> str | None:
    """Return why the listing is out, or None if it should go on to evaluation.

    `keywords` is False while only the title is known and the source's own search
    already matched the listing body. `strict` asks the profile for more than a
    loosely related word.
    """
    fill_distance(listing, config)

    if listing.price_cents is not None and listing.price_cents >= config.max_price * 100:
        return "price"

    if listing.distance_mi is not None:
        slack = APPROX_SLACK_MI if listing.extra.get("distance_approx") else 0
        if listing.distance_mi > config.radius_miles + slack:
            return "distance"
    else:
        # A place that could not be looked up, in a state nowhere near the search.
        state = state_of(listing.location_text)
        if state and state not in nearby_states(config.home, config.radius_miles + APPROX_SLACK_MI):
            return "distance"

    if listing.pickup is False:
        return "no_pickup"

    if keywords and not config.profile.relevant(
        listing.title, listing.description or "", strict=strict
    ):
        return "keyword"

    return None
