"""craigslist, via the JSON endpoint its own search page uses.

One radius search around the home postal code returns postings from every
craigslist site in range, so there is no per-site loop. The endpoint is
undocumented; the item layout handled here was observed in November 2026.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from ..models import Listing
from .base import (
    ListingGone,
    Source,
    SourceError,
    html_to_text,
    http_client,
    raise_for_block,
)

log = logging.getLogger(__name__)

SEARCH_URL = "https://sapi.craigslist.org/web/v8/postings/search/full"
POSTING_URL = "https://www.craigslist.org/view/d/{slug}/{token}"
IMAGE_URL = "https://images.craigslist.org/{image_id}_600x450.jpg"

# Tags of the [tag, value, ...] arrays inside each search item.
TAG_IMAGES, TAG_SLUG, TAG_PRICE_TEXT, TAG_TOKEN = 4, 6, 10, 13


def parse_search(payload: dict) -> list[Listing]:
    data = payload.get("data") or {}
    if not isinstance(data.get("items"), list):
        raise SourceError("craigslist search response has no item list")
    if not data["items"]:
        return []  # a search with no results also has no decode table
    decode = data.get("decode") or {}
    if not isinstance(decode, dict) or "minPostingId" not in decode:
        raise SourceError("craigslist search response has no decode table")

    descriptions = decode.get("locationDescriptions") or []
    first_nearby = data.get("firstNearbyResultId")
    listings: list[Listing] = []
    failures = 0

    for item in data["items"]:
        try:
            posting_id = decode["minPostingId"] + item[0]
            # Results past the requested radius are appended after this id.
            if first_nearby is not None and posting_id == first_nearby:
                break
            tagged = {el[0]: el[1:] for el in item if isinstance(el, list) and el}
            slug, token = tagged[TAG_SLUG][0], tagged[TAG_TOKEN][0]
            title = item[-1]
            if not isinstance(title, str):
                raise ValueError("no title")

            location, lat, lon = "", None, None
            if isinstance(item[4], str) and "~" in item[4]:
                indexes, lat_text, lon_text = item[4].split("~")[:3]
                lat, lon = float(lat_text), float(lon_text)
                parts = indexes.split(":")
                if len(parts) > 1 and int(parts[1]) < len(descriptions):
                    location = str(descriptions[int(parts[1])] or "")

            listings.append(
                Listing(
                    source="craigslist",
                    source_id=str(posting_id),
                    url=POSTING_URL.format(slug=slug, token=token),
                    title=title.strip(),
                    # Postings without a price have no price tag; the bare integer is then unreliable.
                    price_cents=item[3] * 100 if TAG_PRICE_TEXT in tagged else None,
                    location_text=location,
                    lat=lat,
                    lon=lon,
                    image_urls=[
                        IMAGE_URL.format(image_id=str(image).split(":", 1)[-1])
                        for image in tagged.get(TAG_IMAGES, [])
                    ],
                    pickup=True,
                    posted_at=datetime.fromtimestamp(
                        decode["minPostedDate"] + item[1], timezone.utc
                    ).isoformat(timespec="seconds"),
                )
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            failures += 1
            log.debug("craigslist: skipped an item that did not parse: %s", exc)

    if failures and not listings:
        raise SourceError(f"craigslist: none of {failures} search items could be parsed")
    if failures:
        log.warning("craigslist: %d search items could not be parsed", failures)
    return listings


def parse_posting(page: str) -> dict:
    """Pull body text, attributes, coordinates and original post time out of a posting page."""
    body_match = re.search(r'<section id="postingbody">(.*?)</section>', page, re.S)
    if not body_match:
        if re.search(r"this posting has (been|expired)", page, re.I):
            raise ListingGone()
        raise SourceError("craigslist posting page has no body section")
    body = re.sub(r'<div class="print-information.*?</div>\s*</div>', "", body_match.group(1), flags=re.S)

    attributes = []
    for label, value in re.findall(
        r'<span class="labl">(.*?)</span>\s*<span class="valu">(.*?)</span>', page, re.S
    ):
        attributes.append(f"{html_to_text(label)} {html_to_text(value)}")

    result = {"description": html_to_text(body)}
    if attributes:
        result["description"] += "\n\n" + "\n".join(attributes)

    coords = re.search(r'data-latitude="([-\d.]+)"\s+data-longitude="([-\d.]+)"', page)
    if coords:
        result["lat"], result["lon"] = float(coords.group(1)), float(coords.group(2))
    posted = re.search(r'posted:\s*<time class="date timeago" datetime="([^"]+)"', page)
    if posted:
        result["posted_at"] = posted.group(1)
    return result


class Craigslist(Source):
    name = "craigslist"

    def __init__(self, config, source_config):
        super().__init__(config, source_config)
        self.http = http_client()

    def search(self, query: str) -> list[Listing]:
        self.pause()
        response = self.http.get(
            SEARCH_URL,
            params={
                # The first number is a craigslist area id; `postal` and `search_distance` override it.
                "batch": "12-0-360-0-0",
                "cc": "US",
                "lang": "en",
                "searchPath": "sss",
                "query": query,
                "max_price": self.config.max_price,
                "postal": self.config.postal_code,
                "search_distance": int(self.config.radius_miles),
            },
        )
        raise_for_block(response)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError("craigslist search did not return JSON") from exc
        return parse_search(payload)

    def enrich(self, listing: Listing) -> None:
        self.pause()
        response = self.http.get(listing.url)
        if response.status_code in (404, 410):
            raise ListingGone()
        raise_for_block(response)
        detail = parse_posting(response.text)
        listing.description = detail["description"]
        listing.lat = detail.get("lat", listing.lat)
        listing.lon = detail.get("lon", listing.lon)
        listing.posted_at = detail.get("posted_at", listing.posted_at)

    def close(self) -> None:
        self.http.close()
