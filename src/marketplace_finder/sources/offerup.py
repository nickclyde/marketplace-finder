"""OfferUp, via the GraphQL endpoint its website uses. No login is involved.

The endpoint is undocumented; query and field names were observed in November 2026.
"""

from __future__ import annotations

from ..models import Listing
from .base import (
    ListingGone,
    Source,
    SourceError,
    dollars_to_cents,
    http_client,
    raise_for_block,
)

GRAPHQL_URL = "https://offerup.com/api/graphql"
ITEM_URL = "https://offerup.com/item/detail/{listing_id}"

SEARCH_QUERY = """
query GetModularFeed($searchParams: [SearchParam]) {
  modularFeed(params: $searchParams) {
    looseTiles {
      __typename
      ... on ModularFeedTileListing {
        tileType
        listing { listingId title price locationName flags image { url } }
      }
    }
  }
}
"""

DETAIL_QUERY = """
query GetListingDetailByListingId($listingId: ID) {
  listing(listingId: $listingId) {
    listingId title description price postDate state isRemoved
    locationDetails { locationName latitude longitude }
    photos { detail { url } }
    fulfillmentDetails { localPickupEnabled shippingEnabled }
  }
}
"""


def parse_search(payload: dict) -> list[Listing]:
    try:
        tiles = payload["data"]["modularFeed"]["looseTiles"]
    except (KeyError, TypeError) as exc:
        raise SourceError(f"offerup search response has no tiles: {_errors(payload)}") from exc

    listings = []
    for tile in tiles or []:
        item = tile.get("listing")
        if tile.get("tileType") != "LISTING" or not item or not item.get("listingId"):
            continue  # ads and promo tiles
        image = (item.get("image") or {}).get("url")
        listings.append(
            Listing(
                source="offerup",
                source_id=item["listingId"],
                url=ITEM_URL.format(listing_id=item["listingId"]),
                title=(item.get("title") or "").strip(),
                price_cents=dollars_to_cents(item.get("price")),
                location_text=item.get("locationName") or "",
                image_urls=[image] if image else [],
                pickup="LOCAL_PICKUP" in (item.get("flags") or []),
            )
        )
    return listings


def apply_detail(listing: Listing, payload: dict) -> None:
    try:
        item = payload["data"]["listing"]
    except (KeyError, TypeError) as exc:
        raise SourceError(f"offerup detail response has no listing: {_errors(payload)}") from exc
    if not item or item.get("isRemoved") or item.get("state") not in (None, "LISTED"):
        raise ListingGone()

    listing.title = (item.get("title") or listing.title).strip()
    listing.description = (item.get("description") or "").strip()
    listing.price_cents = dollars_to_cents(item.get("price")) or listing.price_cents
    listing.posted_at = item.get("postDate") or listing.posted_at

    location = item.get("locationDetails") or {}
    listing.location_text = location.get("locationName") or listing.location_text
    try:
        listing.lat, listing.lon = float(location["latitude"]), float(location["longitude"])
    except (KeyError, TypeError, ValueError):
        pass

    photos = [p["detail"]["url"] for p in item.get("photos") or [] if (p.get("detail") or {}).get("url")]
    if photos:
        listing.image_urls = photos
    fulfillment = item.get("fulfillmentDetails") or {}
    if fulfillment.get("localPickupEnabled") is not None:
        listing.pickup = bool(fulfillment["localPickupEnabled"])


def _errors(payload) -> str:
    if isinstance(payload, dict) and payload.get("errors"):
        return "; ".join(str(e.get("message", e)) for e in payload["errors"])[:300]
    return "unexpected shape"


class OfferUp(Source):
    name = "offerup"

    def __init__(self, config, source_config):
        super().__init__(config, source_config)
        self.http = http_client(headers={"Origin": "https://offerup.com"})

    def _post(self, operation: str, query: str, variables: dict) -> dict:
        self.pause()
        response = self.http.post(
            GRAPHQL_URL, json={"operationName": operation, "variables": variables, "query": query}
        )
        raise_for_block(response)
        try:
            return response.json()
        except ValueError as exc:
            raise SourceError("offerup did not return JSON") from exc

    def search(self, query: str) -> list[Listing]:
        params = {
            "q": query,
            "lat": f"{self.config.latitude:.4f}",
            "lon": f"{self.config.longitude:.4f}",
            # OfferUp caps the radius at its own "Maximum"; distance is re-checked in code.
            "radius": str(int(self.config.radius_miles)),
            "PRICE_MAX": str(self.config.max_price),
            "SORT": "-posted",
            "platform": "web",
            "limit": "50",
        }
        payload = self._post(
            "GetModularFeed",
            SEARCH_QUERY,
            {"searchParams": [{"key": k, "value": v} for k, v in params.items()]},
        )
        return parse_search(payload)

    def enrich(self, listing: Listing) -> None:
        payload = self._post(
            "GetListingDetailByListingId", DETAIL_QUERY, {"listingId": listing.source_id}
        )
        apply_detail(listing, payload)

    def close(self) -> None:
        self.http.close()
