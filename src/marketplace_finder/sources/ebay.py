"""eBay, via the official Browse API with local-pickup filters.

Needs a production keyset (EBAY_CLIENT_ID / EBAY_CLIENT_SECRET in .env); until
those are set the source reports itself as skipped.
"""

from __future__ import annotations

from urllib.parse import quote

from ..models import Listing
from .base import (
    ListingGone,
    Source,
    SourceBlocked,
    SourceError,
    SourceSkipped,
    dollars_to_cents,
    html_to_text,
    http_client,
)

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
ITEM_URL = "https://api.ebay.com/buy/browse/v1/item/{item_id}"
SCOPE = "https://api.ebay.com/oauth/api_scope"
MAX_DESCRIPTION_CHARS = 6000
KM_PER_MILE = 1.609344


def parse_search(payload: dict) -> list[Listing]:
    listings = []
    for item in payload.get("itemSummaries") or []:
        item_id = item.get("itemId")
        if not item_id:
            continue
        options = item.get("buyingOptions") or []
        # For auctions the amount that matters now is the current bid.
        price = (item.get("currentBidPrice") or item.get("price") or {}).get("value")

        location = item.get("itemLocation") or {}
        place = ", ".join(p for p in (location.get("city"), location.get("stateOrProvince")) if p)

        distance = None
        pickup_distance = item.get("distanceFromPickupLocation") or {}
        if pickup_distance.get("value") is not None:
            distance = float(pickup_distance["value"])
            if str(pickup_distance.get("unitOfMeasure", "mi")).lower().startswith("km"):
                distance /= KM_PER_MILE

        images = [(item.get("image") or {}).get("imageUrl")]
        images += [image.get("imageUrl") for image in item.get("additionalImages") or []]

        listings.append(
            Listing(
                source="ebay",
                source_id=str(item.get("legacyItemId") or item_id),
                url=item.get("itemWebUrl") or f"https://www.ebay.com/itm/{item.get('legacyItemId')}",
                title=(item.get("title") or "").strip(),
                price_cents=dollars_to_cents(price),
                location_text=place or str(location.get("postalCode") or ""),
                distance_mi=None if distance is None else round(distance, 1),
                image_urls=[url for url in images if url],
                description=item.get("shortDescription") or "",
                pickup=True,  # the search is restricted to local-pickup listings
                posted_at=item.get("itemCreationDate") or "",
                extra={"item_id": item_id, "buying_options": options},
            )
        )
    return listings


def apply_detail(listing: Listing, item: dict) -> None:
    lines = []
    options = item.get("buyingOptions") or listing.extra.get("buying_options") or []
    if "AUCTION" in options:
        bid = (item.get("currentBidPrice") or {}).get("value")
        lines.append(
            "Listing type: auction"
            + (f", current bid ${bid}" if bid else "")
            + (f", {item['bidCount']} bids" if item.get("bidCount") is not None else "")
            + (f", ends {item['itemEndDate']}" if item.get("itemEndDate") else "")
        )
        listing.price_cents = dollars_to_cents(bid) or listing.price_cents
    if item.get("condition"):
        lines.append(f"Condition: {item['condition']}")
    if item.get("conditionDescription"):
        lines.append(f"Seller's condition notes: {item['conditionDescription']}")
    for aspect in item.get("localizedAspects") or []:
        if aspect.get("name") and aspect.get("value"):
            lines.append(f"{aspect['name']}: {aspect['value']}")

    body = html_to_text(item.get("description") or "")[:MAX_DESCRIPTION_CHARS]
    listing.description = "\n".join(lines) + ("\n\n" + body if body else "")

    images = [(item.get("image") or {}).get("imageUrl")]
    images += [image.get("imageUrl") for image in item.get("additionalImages") or []]
    images = [url for url in images if url]
    if images:
        listing.image_urls = images


class Ebay(Source):
    name = "ebay"
    delay = (0.5, 1.0)

    def __init__(self, config, source_config):
        super().__init__(config, source_config)
        self.http = http_client()
        self._token: str | None = None

    def _headers(self) -> dict:
        if not (self.config.ebay_client_id and self.config.ebay_client_secret):
            raise SourceSkipped("eBay API keys are not set in .env")
        if self._token is None:
            response = self.http.post(
                TOKEN_URL,
                auth=(self.config.ebay_client_id, self.config.ebay_client_secret),
                data={"grant_type": "client_credentials", "scope": SCOPE},
            )
            if response.status_code in (400, 401):
                raise SourceError(f"eBay rejected the API keys: {response.text[:200]}")
            self._check(response)
            self._token = response.json()["access_token"]
        location = quote(f"country=US,zip={self.config.postal_code}", safe="")
        return {
            "Authorization": f"Bearer {self._token}",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
            "X-EBAY-C-ENDUSERCTX": f"contextualLocation={location}",
        }

    @staticmethod
    def _check(response) -> None:
        if response.status_code == 429:
            raise SourceBlocked("eBay API rate limit reached")
        if response.status_code >= 400:
            raise SourceError(f"eBay API HTTP {response.status_code}: {response.text[:300]}")

    def search(self, query: str) -> list[Listing]:
        headers = self._headers()
        filters = [
            f"price:[..{self.config.max_price}]",
            "priceCurrency:USD",
            "buyingOptions:{FIXED_PRICE|AUCTION|BEST_OFFER|CLASSIFIED_AD}",
            "deliveryOptions:{SELLER_ARRANGED_LOCAL_PICKUP}",
            "pickupCountry:US",
            f"pickupPostalCode:{self.config.postal_code}",
            f"pickupRadius:{int(self.config.radius_miles)}",
            "pickupRadiusUnit:mi",
        ]
        self.pause()
        response = self.http.get(
            SEARCH_URL,
            headers=headers,
            params={
                "q": query,
                "filter": ",".join(filters),
                "sort": "newlyListed",
                "limit": 100,
                "fieldgroups": "EXTENDED",
            },
        )
        self._check(response)
        return parse_search(response.json())

    def enrich(self, listing: Listing) -> None:
        headers = self._headers()
        self.pause()
        item_id = listing.extra.get("item_id") or f"v1|{listing.source_id}|0"
        response = self.http.get(ITEM_URL.format(item_id=quote(item_id, safe="")), headers=headers)
        if response.status_code == 404:
            raise ListingGone()
        self._check(response)
        apply_detail(listing, response.json())

    def close(self) -> None:
        self.http.close()
