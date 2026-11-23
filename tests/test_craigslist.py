import pytest

from marketplace_finder.sources.base import ListingGone, SourceError
from marketplace_finder.sources.craigslist import parse_posting, parse_search


def test_search_decodes_items_and_drops_out_of_radius_extras(fixture_json):
    payload = fixture_json("craigslist_search.json")
    listings = parse_search(payload)

    # The response carries 14 items; the last is a "nearby" extra beyond the radius.
    assert len(payload["data"]["items"]) == 14
    assert len(listings) == payload["data"]["totalResultCount"] == 13
    assert str(payload["data"]["firstNearbyResultId"]) not in {l.source_id for l in listings}

    first = listings[0]
    assert first.source_id == "7944825698"  # matches "post id" on the posting page
    assert first.title.startswith("Sony Trinitron KV-27FS120 27\" flat tube TV")
    assert first.price_cents == 14000
    assert first.url == (
        "https://www.craigslist.org/view/d/"
        "davis-sony-trinitron-kv-27fs120-27-flat/xaNSCbULiEkg2SF3zvY5F7"
    )
    assert (first.lat, first.lon) == (38.545, -121.74)
    assert first.location_text == "Davis"
    assert first.image_urls[0] == "https://images.craigslist.org/00303_L1PTFyCVCgF_0CI0t2_600x450.jpg"
    assert len(first.image_urls) == 6
    assert first.pickup is True


def test_search_reads_three_part_location(fixture_json):
    listings = parse_search(fixture_json("craigslist_search.json"))
    berkeley = next(l for l in listings if "KV-13M42" in l.title)
    assert berkeley.location_text == "berkeley"
    assert berkeley.lat == pytest.approx(37.872)


def test_search_rejects_unrecognised_payload():
    with pytest.raises(SourceError):
        parse_search({"data": {"unexpected": True}})


def test_search_fails_loudly_when_no_item_parses(fixture_json):
    payload = fixture_json("craigslist_search.json")
    payload["data"]["items"] = [["not", "an", "item"]]
    with pytest.raises(SourceError):
        parse_search(payload)


def test_posting_page(fixture_text):
    detail = parse_posting(fixture_text("craigslist_posting.html"))
    assert "Model KV-27FS120" in detail["description"]
    assert "QR Code" not in detail["description"]
    assert "PLEASE REPLY THROUGH CRAIGSLIST EMAIL ONLY" in detail["description"]  # full body, not the truncated JSON-LD
    assert "condition: excellent" in detail["description"]
    assert "model name / number: KV-27FS120" in detail["description"]
    assert (detail["lat"], detail["lon"]) == (38.545, -121.74)
    assert detail["posted_at"] == "2026-08-14T09:42:37-0700"


def test_deleted_posting_is_gone():
    with pytest.raises(ListingGone):
        parse_posting("<html><body><h2>This posting has been deleted by its author.</h2></body></html>")


def test_search_with_no_results(fixture_json):
    # Observed for a query with zero matches: "items": [] and "decode": 0.
    payload = fixture_json("craigslist_search_empty.json")
    assert payload["data"]["items"] == [] and payload["data"]["decode"] == 0
    assert parse_search(payload) == []
