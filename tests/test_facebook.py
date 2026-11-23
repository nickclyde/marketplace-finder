"""Facebook JSON extraction, against payloads shaped like the ones third-party tools document."""

import pytest

from marketplace_finder.sources.base import ListingGone
from marketplace_finder.sources.facebook import apply_detail, extract_listings, parse_json_blobs


def node(listing_id, title, **extra):
    return {"node": {"listing": {
        "id": listing_id,
        "marketplace_listing_title": title,
        "listing_price": {"amount": "150.00", "formatted_amount": "$150"},
        "location": {"reverse_geocode": {"city": "Elk Grove", "state": "CA"}},
        "primary_listing_photo": {"image": {"uri": "https://scontent.example/p.jpg"}},
        "is_sold": False,
        "is_pending": False,
        **extra,
    }}}


def test_listings_are_found_at_any_depth_and_deduplicated():
    blobs = [
        {"require": [["x", {"__bbox": {"result": {"data": {"marketplace_search": {"feed_units": {
            "edges": [node("111", "Sony Trinitron 27 inch"), node("222", "Sold one", is_sold=True)]
        }}}}}}]]},
        {"data": {"edges": [node("111", "Sony Trinitron 27 inch"), node("333", "Sony WEGA")]}},
    ]
    listings = extract_listings(blobs)
    assert sorted(l.source_id for l in listings) == ["111", "333"]
    first = next(l for l in listings if l.source_id == "111")
    assert first.price_cents == 15000
    assert first.location_text == "Elk Grove, CA"
    assert first.url == "https://www.facebook.com/marketplace/item/111/"
    assert first.image_urls == ["https://scontent.example/p.jpg"]


def test_newline_separated_responses():
    blobs = parse_json_blobs('{"a": 1}\n{"b": 2}\nnot json')
    assert blobs == [{"a": 1}, {"b": 2}]


def test_detail(make_listing):
    listing = make_listing(source="facebook", source_id="111", lat=None, lon=None)
    found = apply_detail(listing, [{"data": {"viewer": {"target": {
        "id": "111",
        "redacted_description": {"text": "Works. Component inputs on back."},
        "listing_photos": [{"image": {"uri": "https://scontent.example/1.jpg"}},
                           {"image": {"uri": "https://scontent.example/2.jpg"}}],
        "location": {"latitude": 38.41, "longitude": -121.37},
        "creation_time": 1794500000,
    }}}}])
    assert found
    assert listing.description == "Works. Component inputs on back."
    assert len(listing.image_urls) == 2
    assert (listing.lat, listing.lon) == (38.41, -121.37)
    assert listing.posted_at.startswith("2026-11-")


def test_detail_ignores_other_listings_and_reports_nothing_found(make_listing):
    listing = make_listing(source="facebook", source_id="111")
    assert not apply_detail(listing, [{"id": "999", "redacted_description": {"text": "other"}}])
    assert listing.description == ""


def test_sold_detail_is_gone(make_listing):
    with pytest.raises(ListingGone):
        apply_detail(make_listing(source="facebook", source_id="111"),
                     [{"id": "111", "redacted_description": {"text": "x"}, "is_sold": True}])
