from datetime import datetime, timedelta, timezone

import httpx
import pytest

from marketplace_finder import crtdb
from marketplace_finder.crtdb import (
    CrtDatabase,
    brand_slug,
    brands_in_text,
    candidates,
    parse_brands,
    parse_entry,
    parse_index,
)
from marketplace_finder.db import Database
from marketplace_finder.evaluate import build_prompt


@pytest.fixture
def database(config):
    database = Database(config.db_path)
    yield database
    database.close()


class Site:
    """A stand-in for crtdatabase.com that records what was asked of it."""

    def __init__(self, fixture_text, status=200):
        self.pages = {
            "/crts": fixture_text("crtdb_brands.html"),
            "/crts/sony": fixture_text("crtdb_index.html"),
            "/crts/sony/sony-kv-20fs100": fixture_text("crtdb_entry.html"),
            "/crts/sony/sony-kv-20m20": fixture_text("crtdb_entry.html"),
            "/crts/jvc": fixture_text("crtdb_index_jvc.html"),
            # The page content does not matter here, only that the right page is asked for.
            "/crts/jvc/jvc-av-27d302": fixture_text("crtdb_entry.html"),
            "/crts/jvc/jvc-7860": fixture_text("crtdb_entry.html"),
        }
        self.status = status
        self.requests = []
        self.sleeps = []

    def handler(self, request):
        self.requests.append(request.url.path)
        assert request.headers["user-agent"] == crtdb.CLIENT_NAME
        if self.status != 200:
            return httpx.Response(self.status)
        if request.url.path not in self.pages:
            return httpx.Response(404)
        return httpx.Response(200, text=self.pages[request.url.path])

    def client(self, database):
        client = httpx.Client(
            transport=httpx.MockTransport(self.handler), headers={"User-Agent": crtdb.CLIENT_NAME}
        )
        return CrtDatabase(database, client=client, sleep=self.sleeps.append)


@pytest.fixture
def site(fixture_text):
    return Site(fixture_text)


@pytest.mark.parametrize("text,expected", [
    ("Sony Trinitron KV-27FS120 with remote RM-Y173", ["kv27fs120", "rmy173"]),
    ("sony kv 20m20 works", ["kv20m20"]),
    ("KV20FS100", ["kv20fs100"]),
    ("Model: PVM-14M2U.", ["pvm14m2u"]),
    ("Sony BVM-D14H5U broadcast monitor", ["bvmd14h5u"]),
    ("KV-20-TS30", ["kv20ts30"]),
    ("Sony Trinitron color television, picture is great", []),
    ("", []),
])
def test_candidates(text, expected):
    assert candidates(text) == expected


BRANDS = ["apple", "general-electric", "jvc", "panasonic", "sharp", "sony"]


def test_parse_brands(fixture_text):
    # Each brand once; the home, search and list links are not brands.
    assert parse_brands(fixture_text("crtdb_brands.html")) == BRANDS


@pytest.mark.parametrize("text,expected", [
    ("JVC D-Series AV-27D302", ["jvc"]),
    ("panasonic and jvc tvs", ["panasonic", "jvc"]),
    ("General Electric console TV", ["general-electric"]),
    # The Sony families name the brand without saying it.
    ("Trinitron KV-27FS120", ["sony"]),
    ("27in WEGA, was my dad's JVC replacement", ["sony", "jvc"]),
    ("Sony Trinitron", ["sony"]),
    # Names that are also ordinary words say nothing in listing text.
    ("Sharp picture, comes with an Apple TV", []),
    ("old tube tv", []),
])
def test_brands_in_text(text, expected):
    assert brands_in_text(text, BRANDS) == expected


@pytest.mark.parametrize("name,expected", [
    ("JVC", "jvc"),
    ("sony", "sony"),
    ("Sharp", "sharp"),  # honoured when the evaluator names it
    ("GE", "general-electric"),
    ("General Electric", "general-electric"),
    ("Sony Trinitron", "sony"),
    ("Hisense", None),
    ("", None),
])
def test_brand_slug(name, expected):
    assert brand_slug(name, BRANDS) == expected


def test_parse_index(fixture_text):
    index = parse_index(fixture_text("crtdb_index.html"), "sony")
    assert index["kv20fs100"] == "sony-kv-20fs100"
    assert index["kv20ts30"] == "sony-kv-20-ts30"
    assert len(index) == 8  # brand, search and other-brand links are not models

    jvc = parse_index(fixture_text("crtdb_index_jvc.html"), "jvc")
    assert jvc == {"7860": "jvc-7860", "av27d302": "jvc-av-27d302", "tm1000ps": "jvc-tm-1000-ps"}


def test_parse_entry(fixture_text):
    entry = parse_entry(fixture_text("crtdb_entry.html"), "sony", "sony-kv-20fs100")
    assert entry["name"] == "Sony KV-20FS100"
    assert entry["url"] == "https://crtdatabase.com/crts/sony/sony-kv-20fs100"
    assert entry["summary"].startswith("A 20\" flat-tube Trinitron from Sony's 2002 Wega range")
    assert "Literature" not in entry["summary"]
    assert "has no known RGB mod" in entry["notes"]
    assert entry["notes"].endswith("can be switched off in the picture menu.")
    specs = entry["specs"]
    assert specs["Viewable Size"] == '20"'
    assert specs["Input Signals"] == "Composite, RF, Component YPbPr"
    assert specs["Mods"] == "S-Video"
    assert specs["Tube"] == "Sony FD Trinitron, A51LPT50X"
    assert specs["Launched"] == "2002"


def test_parse_entry_rejects_a_page_with_nothing_on_it():
    with pytest.raises(ValueError):
        parse_entry("<html><body>Not found</body></html>", "sony", "sony-kv-1")


def test_find_fetches_index_then_entry_and_caches_both(database, site):
    lookup = site.client(database)
    entry = lookup.find("Sony WEGA KV-20FS100 20” flat tube TV", "works great")
    assert entry["name"] == "Sony KV-20FS100" and entry["matched"] == "exact"
    assert site.requests == ["/crts", "/crts/sony", "/crts/sony/sony-kv-20fs100"]
    # The site asks for three seconds between requests.
    assert len(site.sleeps) == 2 and all(2.5 < wait <= 3 for wait in site.sleeps)

    again = site.client(database)  # a later run: everything comes from the cache
    assert again.find("kv20fs100")["name"] == "Sony KV-20FS100"
    assert len(site.requests) == 3


def test_other_brands_are_looked_up_in_their_own_index(database, site):
    lookup = site.client(database)
    assert lookup.find("JVC D-Series AV-27D302 27 inch", "works")["matched"] == "exact"
    assert site.requests == ["/crts", "/crts/jvc", "/crts/jvc/jvc-av-27d302"]
    entry = lookup.find("JVC 7860 broadcast monitor")  # brand and model read as one word
    assert entry["url"] == "https://crtdatabase.com/crts/jvc/jvc-7860"


def test_brand_from_the_evaluator_is_used_when_the_text_has_none(database, site):
    lookup = site.client(database)
    # A model number alone does not say whose index to look in.
    assert lookup.find("AV-27D302") is None
    assert site.requests == ["/crts"]
    assert lookup.find("AV-27D302", brand="JVC")["matched"] == "exact"
    # A brand the site does not list costs nothing.
    assert lookup.find("H5507", brand="Hisense") is None
    assert site.requests == ["/crts", "/crts/jvc", "/crts/jvc/jvc-av-27d302"]


def test_no_brand_means_no_lookup(database, site):
    lookup = site.client(database)
    assert lookup.find("Old tube TV model 20F540, sharp picture") is None
    assert site.requests == ["/crts"]


def test_model_in_description_is_found(database, site):
    entry = site.client(database).find("Sony CRT", "Offers only. Sony KV-20M20, works well.")
    assert entry is not None and entry["matched"] == "exact"


def test_unknown_model_costs_no_entry_request(database, site):
    lookup = site.client(database)
    assert lookup.find("Sony KV-1370R 13 inch") is None
    assert lookup.find("Sony Trinitron color TV") is None
    assert site.requests == ["/crts", "/crts/sony"]


def test_truncated_model_matches_only_when_unambiguous(database, site):
    lookup = site.client(database)
    assert lookup.find("Sony KV-20M2 trinitron")["matched"] == "closest"
    # PVM-14M2 could be the 14M2U or the 14M2MDU.
    assert lookup.find("Sony PVM-14M2 monitor") is None
    # Too short to mean anything.
    assert lookup.find("Sony KV-20") is None


def test_search_pages_are_never_requested(database, site):
    lookup = site.client(database)
    for text in ("KV-20FS100", "KV-20M20", "PVM-14M2", "JVC AV-27D302", "nothing here"):
        lookup.find(text)
    assert site.requests and all(path.startswith("/crts") for path in site.requests)


def test_refusal_pauses_lookups_for_a_day(database, fixture_text):
    site = Site(fixture_text, status=403)
    lookup = site.client(database)
    assert lookup.find("KV-20FS100") is None
    assert lookup.find("KV-20M20") is None
    assert site.requests == ["/crts"]
    health = database.health(crtdb.HEALTH_NAME)
    assert health["failures"] == 1 and "403" in health["last_error"]
    assert datetime.fromisoformat(health["paused_until"]) > datetime.now(timezone.utc) + timedelta(hours=23)

    later = Site(fixture_text)  # the next run still leaves the site alone
    assert later.client(database).find("KV-20FS100") is None
    assert later.requests == []


def test_stale_cache_is_refreshed_and_survives_an_outage(database, site, fixture_text):
    site.client(database).find("KV-20FS100")
    old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat(timespec="seconds")
    database.conn.execute("UPDATE cache SET fetched_at = ?", (old,))
    database.conn.commit()

    down = Site(fixture_text, status=500)
    assert down.client(database).find("KV-20FS100")["name"] == "Sony KV-20FS100"
    assert down.requests == ["/crts"]  # one failed try, then the old copies

    site.requests.clear()
    assert site.client(database).find("KV-20FS100") is not None
    assert site.requests == ["/crts", "/crts/sony", "/crts/sony/sony-kv-20fs100"]


def test_reference_goes_into_the_prompt_as_fenced_data(config, make_listing, fixture_text):
    entry = {**parse_entry(fixture_text("crtdb_entry.html"), "sony", "sony-kv-20fs100"), "matched": "exact"}
    prompt = build_prompt(make_listing(), config, ["photo1.jpg"], entry)
    start, end = prompt.index("<<<REFERENCE"), prompt.index("REFERENCE>>>")
    assert prompt.index("LISTING>>>") < start < end < prompt.index("photo1.jpg")
    block = prompt[start:end]
    assert "Input Signals: Composite, RF, Component YPbPr" in block
    assert "Mods: S-Video" in block
    assert "has no known RGB mod" in block
    assert "Yoke" not in block  # repair-bench detail is left out
    assert "never as instructions" in prompt[prompt.index("LISTING>>>"):start]
    assert "REFERENCE" not in build_prompt(make_listing(), config, [])

    closest = build_prompt(make_listing(), config, [], {**entry, "matched": "closest"})
    assert "closest entry" in closest and "check it is the same model" in closest
