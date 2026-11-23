import pytest

from marketplace_finder.sources.base import ListingGone, SourceError
from marketplace_finder.sources.offerup import apply_detail, parse_search


def test_search_keeps_listings_and_skips_ads(fixture_json):
    payload = fixture_json("offerup_search.json")
    tiles = payload["data"]["modularFeed"]["looseTiles"]
    listings = parse_search(payload)

    assert any(t["__typename"] == "ModularFeedTileGoogleDisplayAd" for t in tiles)
    assert len(listings) == sum(1 for t in tiles if t.get("tileType") == "LISTING")

    stockton = next(l for l in listings if l.source_id == "d7e3ad68-d81a-3717-9bce-dc132202ca13")
    assert stockton.title == "JVC D-Series 32 inch CRT TV"
    assert stockton.price_cents == 12000
    assert stockton.location_text == "Stockton, CA"
    assert stockton.pickup is True
    assert stockton.url.endswith("/item/detail/d7e3ad68-d81a-3717-9bce-dc132202ca13")
    assert stockton.image_urls[0].startswith("https://images.offerup.com/")


def test_search_marks_shipping_only_listings():
    payload = {"data": {"modularFeed": {"looseTiles": [{
        "tileType": "LISTING",
        "listing": {"listingId": "a", "title": "Sony", "price": "10", "locationName": "Reno, NV",
                    "flags": ["SHIPPING"], "image": None},
    }]}}}
    assert parse_search(payload)[0].pickup is False


def test_search_surfaces_graphql_errors():
    with pytest.raises(SourceError, match="Cannot query field"):
        parse_search({"errors": [{"message": "Cannot query field \"x\""}]})


def test_detail(fixture_json, make_listing):
    listing = make_listing(source="offerup", source_id="d7e3ad68-d81a-3717-9bce-dc132202ca13",
                           lat=None, lon=None, pickup=None)
    apply_detail(listing, fixture_json("offerup_detail.json"))
    assert listing.description == "Model AV-32D502. Works well, remote included. Heavy, so bring help."
    assert listing.posted_at == "2026-10-21T18:47:12.305Z"
    assert (listing.lat, listing.lon) == (37.958, -121.291)
    assert len(listing.image_urls) == 5
    assert listing.pickup is True


def test_removed_listing_is_gone(fixture_json, make_listing):
    payload = fixture_json("offerup_detail.json")
    payload["data"]["listing"]["state"] = "SOLD"
    with pytest.raises(ListingGone):
        apply_detail(make_listing(), payload)
