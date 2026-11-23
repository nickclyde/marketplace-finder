import json
from pathlib import Path

import pytest

from marketplace_finder.config import load_config
from marketplace_finder.models import Listing

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


# What the tests run against: the CRT profile, searching around Sacramento, posting with discli.
TEST_CONFIG = """
[profile]
name = "crt"

[search]
postal_code = "95814"
latitude = 38.5816
longitude = -121.4944
radius_miles = 60
area = "Sacramento"
max_price = 500
quiet_hours = [0, 5]
timezone = "America/Los_Angeles"

[evaluator]
model = "opus"
max_per_run = 30
max_photos = 4

[discord]
enabled = true
via = "discli"
channel = "listings"
max_posts_per_run = 10
top_picks = 10

[sources.craigslist]
enabled = true

[sources.offerup]
enabled = true

[sources.ebay]
enabled = true

[sources.facebook]
enabled = true
location = "sacramento"
days_since_listed = 7
max_detail_pages = 6
"""
SECRETS = (
    "NTFY_TOPIC", "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "DISCORD_WEBHOOK_URL",
    "DISCORD_TEST_WEBHOOK_URL", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY",
)


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETPLACE_FINDER_DATA", str(tmp_path / "data"))
    monkeypatch.delenv("MARKETPLACE_FINDER_CONFIG", raising=False)
    for name in SECRETS:
        monkeypatch.delenv(name, raising=False)
    # Its own directory, so tests never read a real config.toml or .env.
    (tmp_path / "config.toml").write_text(TEST_CONFIG)
    return load_config(tmp_path / "config.toml")


@pytest.fixture
def fixture_json():
    return lambda name: json.loads((FIXTURES / name).read_text())


@pytest.fixture
def fixture_text():
    return lambda name: (FIXTURES / name).read_text()


@pytest.fixture
def make_listing():
    def make(**overrides):
        fields = dict(
            source="craigslist",
            source_id="1",
            url="https://example.org/1",
            title="Sony Trinitron KV-27FS120",
            price_cents=15000,
            location_text="Roseville, CA",
            lat=38.75,
            lon=-121.29,
            pickup=True,
        )
        fields.update(overrides)
        return Listing(**fields)

    return make
