"""eBay parsing, against payloads shaped like the Browse API's documented responses."""

import pytest

from marketplace_finder.sources.base import SourceSkipped
from marketplace_finder.sources.ebay import Ebay, apply_detail, parse_search

SEARCH = {
    "total": 2,
    "itemSummaries": [
        {
            "itemId": "v1|135790246810|0",
            "legacyItemId": "135790246810",
            "title": "Sony Trinitron KV-20FS120 20\" CRT TV",
            "price": {"value": "180.00", "currency": "USD"},
            "buyingOptions": ["FIXED_PRICE", "BEST_OFFER"],
            "itemWebUrl": "https://www.ebay.com/itm/135790246810",
            "image": {"imageUrl": "https://i.ebayimg.com/a.jpg"},
            "additionalImages": [{"imageUrl": "https://i.ebayimg.com/b.jpg"}],
            "itemLocation": {"postalCode": "956**", "country": "US", "city": "Roseville",
                             "stateOrProvince": "California"},
            "distanceFromPickupLocation": {"value": "18.4", "unitOfMeasure": "mi"},
            "itemCreationDate": "2026-11-10T18:00:00.000Z",
            "shortDescription": "Used, tested.",
        },
        {
            "itemId": "v1|246801357911|0",
            "legacyItemId": "246801357911",
            "title": "Sony PVM-14M2U",
            "price": {"value": "600.00", "currency": "USD"},
            "currentBidPrice": {"value": "310.00", "currency": "USD"},
            "buyingOptions": ["AUCTION"],
            "itemWebUrl": "https://www.ebay.com/itm/246801357911",
            "distanceFromPickupLocation": {"value": "120", "unitOfMeasure": "km"},
        },
    ],
}


def test_search_fields():
    fixed, auction = parse_search(SEARCH)
    assert fixed.source_id == "135790246810"
    assert fixed.extra["item_id"] == "v1|135790246810|0"
    assert fixed.price_cents == 18000
    assert fixed.location_text == "Roseville, California"
    assert fixed.distance_mi == 18.4
    assert fixed.image_urls == ["https://i.ebayimg.com/a.jpg", "https://i.ebayimg.com/b.jpg"]
    assert fixed.pickup is True

    assert auction.price_cents == 31000  # current bid, not the listed price
    assert auction.distance_mi == pytest.approx(74.6, abs=0.1)  # km converted


def test_search_with_no_results():
    assert parse_search({"total": 0}) == []


def test_detail_builds_description(make_listing):
    listing = make_listing(source="ebay", extra={"buying_options": ["AUCTION"]})
    apply_detail(listing, {
        "buyingOptions": ["AUCTION"],
        "currentBidPrice": {"value": "310.00"},
        "bidCount": 4,
        "itemEndDate": "2026-11-15T02:00:00.000Z",
        "condition": "Used",
        "localizedAspects": [{"name": "Screen Size", "value": "14 in"}],
        "description": "<p>Works great.<br>RGB and S-Video inputs.</p>",
        "image": {"imageUrl": "https://i.ebayimg.com/full.jpg"},
    })
    assert listing.price_cents == 31000
    assert "Listing type: auction, current bid $310.00, 4 bids" in listing.description
    assert "Screen Size: 14 in" in listing.description
    assert "RGB and S-Video inputs." in listing.description
    assert listing.image_urls == ["https://i.ebayimg.com/full.jpg"]


def test_source_is_skipped_without_keys(config):
    source = Ebay(config, config.sources["ebay"])
    with pytest.raises(SourceSkipped):
        source.search("sony trinitron")
