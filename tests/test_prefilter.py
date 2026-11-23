import pytest

from marketplace_finder import prefilter
from marketplace_finder.geo import haversine_mi, lookup_city


def test_haversine_sacramento_to_san_francisco():
    assert haversine_mi((38.5816, -121.4944), (37.7749, -122.4194)) == pytest.approx(75, abs=2)


def test_city_lookup():
    assert lookup_city("Stockton, CA") == pytest.approx((37.9766, -121.3086))
    assert lookup_city("stockton, ca") is not None
    assert lookup_city("Reno, NV") is not None
    assert lookup_city("Portland, OR") == pytest.approx((45.537, -122.65))
    assert lookup_city("Nowhere At All, CA") is None
    assert lookup_city("mission district") is None
    # Not the Census point out by the Farallon Islands.
    assert lookup_city("San Francisco, CA")[1] > -122.6


def test_passes(config, make_listing):
    listing = make_listing()
    assert prefilter.check(listing, config, keywords=True) is None
    assert listing.distance_mi == pytest.approx(16, abs=2)


@pytest.mark.parametrize("cents,reason", [(49999, None), (50000, "price"), (0, None), (None, None)])
def test_price_must_be_under_the_limit(config, make_listing, cents, reason):
    assert prefilter.check(make_listing(price_cents=cents), config, keywords=False) == reason


def test_distance_from_coordinates(config, make_listing):
    los_angeles = make_listing(lat=34.05, lon=-118.24)
    assert prefilter.check(los_angeles, config, keywords=False) == "distance"


def test_distance_from_city_name_gets_slack(config, make_listing):
    # Berkeley is a little over 60 miles out by city centre: kept, because that is approximate.
    berkeley = make_listing(lat=None, lon=None, location_text="Berkeley, CA")
    assert prefilter.check(berkeley, config, keywords=False) is None
    assert 60 < berkeley.distance_mi < 70
    assert berkeley.extra["distance_approx"] is True
    san_jose = make_listing(lat=None, lon=None, location_text="San Jose, CA")
    assert prefilter.check(san_jose, config, keywords=False) == "distance"


def test_exact_coordinates_get_no_slack(config, make_listing):
    # The same spot as Berkeley's centre, but known exactly.
    listing = make_listing(lat=37.8657, lon=-122.2987)
    assert prefilter.check(listing, config, keywords=False) == "distance"


def test_unknown_location_passes_but_other_states_do_not(config, make_listing):
    unknown = make_listing(lat=None, lon=None, location_text="")
    assert prefilter.check(unknown, config, keywords=False) is None
    assert unknown.distance_mi is None
    far = make_listing(lat=None, lon=None, location_text="Portland, OR")
    assert prefilter.check(far, config, keywords=False) == "distance"


def test_source_reported_distance_is_used(config, make_listing):
    listing = make_listing(lat=None, lon=None, location_text="", distance_mi=75.0)
    assert prefilter.check(listing, config, keywords=False) == "distance"


def test_no_pickup(config, make_listing):
    assert prefilter.check(make_listing(pickup=False), config, keywords=False) == "no_pickup"
    assert prefilter.check(make_listing(pickup=None), config, keywords=False) is None


@pytest.mark.parametrize("title,description,reason", [
    # A CRT word or a model family, whatever the brand.
    ("JVC D-Series CRT", "", None),
    ("27in WEGA flat tube", "", None),
    ("PVM-14M2U monitor", "", None),
    ("KV 27FS120", "", None),
    ("Old tube TV", "", None),
    ("Zenith 25 inch", "console television, picture tube is strong", None),
    # A TV with nothing saying what kind is worth a look.
    ("Toshiba TV 20 inch", "works, has remote", None),
    ("Gaming monitor", "great for retro games", None),
    # Not a TV at all, a flat panel, or something that goes with a TV.
    ("Panasonic Tau", "", "keyword"),
    ("Vintage tube radio", "", "keyword"),
    ("Samsung 55 inch Smart TV", "", "keyword"),
    ("TCL Roku TV", "", "keyword"),
    ('Vizio 50" TV', "", "keyword"),
    ("Tech Craft TV Stand", "Glass, fits 36 inch.", "keyword"),
    ("TV wall mount", "", "keyword"),
    ("VTG JCPENNEY TV/VCR COMBO OPERATION/CARE MANUAL", "", "keyword"),
    ("Extra long component video cable", "for your TV", "keyword"),
    # Coming with one is different.
    ("Toshiba TV with remote", "", None),
    ("Magnavox television, remote and original manual", "", None),
    ("Zenith TV w/ matching stand", "", None),
    # A flat-panel word in the description does not count against an old set.
    ("RCA TV 27 inch", "Heavy. Selling because we bought a smart TV.", None),
])
def test_keywords(config, make_listing, title, description, reason):
    listing = make_listing(title=title, description=description)
    assert prefilter.check(listing, config, keywords=True) == reason
    assert prefilter.check(listing, config, keywords=False) is None


@pytest.mark.parametrize("title,description,keep", [
    # A CRT word or a model family is enough on its own.
    ("Trinitron 27in", "", True),
    ("KV-27FS120", "", True),
    ("PVM 14 inch", "", True),
    ("CRT TV", "", True),
    ("Panasonic crt 20in", "", True),
    ("Old tube TV works great", "", True),
    ("Box TV", "tube television, has a remote", True),
    # A TV the title calls old is worth a look, unless it is plainly a flat panel or not a TV.
    ('Old TV 26"', "", True),
    ("Vintage Zenith television", "", True),
    ("Old Samsung 55 inch TV", "", False),
    ("Old LG smart TV", "", False),
    ("Vintage 1985 ADC CD-100X Compact Disc Player", "", False),
    # A TV word alone is not, unless the set is a Sony.
    ("Samsung TV", "", False),
    ("Toshiba TV 20 inch", "", False),
    ("Samsung 55 inch Smart TV", "", False),
    ("Vintage tube radio", "", False),
    ("Tube amp", "", False),
    # A brand alone is not either.
    ("Sony SRS-XV800 Wireless Party Speaker", "", False),
    ("Sony 28-70 OSS FE 3.5-5.6 e mount", "barely used lens", False),
    ("Sony dream machine & iPod nano", "", False),
    # Sony plus a CRT word.
    ("Sony CRT", "", True),
    ("Sony 20 inch", "old picture tube set, works", True),
    # Sony plus a TV word, unless the title names a flat panel or an accessory.
    ("Sony TV", "", True),
    ("Televisión Sony", "", True),
    ("Sony video monitor", "", True),
    ("Sony Bravia 55” TV", "", False),
    ("Sony KD-50X80K 50-inch Smart TV", "", False),
    ("Sony KDL-40EX400 TV", "", False),
    ("Sony 32 LED TV", "", False),
    ("Sony 65 inch TV", "", False),
    ("Sony TV remote", "", False),
    # Flat-panel words in the description do not count against an old set.
    ("Sony TV 27 inch", "Heavy. Selling because we bought a smart TV.", True),
    ("Sony Trinitron, not LCD", "", True),
    ("Sony flat screen HD TV", "KV-27HS420", True),
])
def test_strict_keywords(config, make_listing, title, description, keep):
    listing = make_listing(title=title, description=description)
    expected = None if keep else "keyword"
    assert prefilter.check(listing, config, keywords=True, strict=True) == expected
