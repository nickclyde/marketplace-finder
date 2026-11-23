"""Facebook Marketplace, through a real browser that stays logged in.

Marketplace search needs a logged-in session, so this drives Chromium with a
persistent profile kept under the data directory. `marketplace-finder fb-login`
opens that profile in a visible window for the one-time manual login.

Listings are read from the JSON the page itself loads (embedded in the HTML and
in its /api/graphql/ responses), found by key name rather than by position, so
layout changes do not matter. If Facebook shows a login or verification page,
the source stops and asks for attention instead of retrying.

The search radius is the one saved on the account (set it in the Marketplace
location dialog during fb-login); Facebook ignores a radius in the URL when
logged in.
"""

from __future__ import annotations

import json
import logging
import os
import random
from datetime import datetime, timezone
from urllib.parse import urlencode

from ..config import Config
from ..models import Listing
from .base import (
    ListingGone,
    Source,
    SourceError,
    SourceNeedsAttention,
    SourceSkipped,
    dollars_to_cents,
)

log = logging.getLogger(__name__)

BASE = "https://www.facebook.com"
LOGIN_MARKERS = ("/login", "/checkpoint", "two_step_verification", "/recover", "/confirmemail")
NEEDS_LOGIN = (
    "Facebook is logged out or asking for verification. "
    "Run `marketplace-finder fb-login` on the machine and sign in again."
)


def walk(node, key: str):
    """Yield every dict anywhere inside `node` that has `key`."""
    stack = [node]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if key in current:
                yield current
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def parse_json_blobs(text: str) -> list:
    """Facebook responses are one JSON document, or several separated by newlines."""
    blobs = []
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                blobs.append(json.loads(line))
            except ValueError:
                continue
    return blobs


def listing_from_node(node: dict) -> Listing | None:
    listing_id, title = node.get("id"), node.get("marketplace_listing_title")
    if not listing_id or not title or node.get("is_sold") or node.get("is_pending"):
        return None
    price = node.get("listing_price") or {}
    geocode = (node.get("location") or {}).get("reverse_geocode") or {}
    place = ", ".join(p for p in (geocode.get("city"), geocode.get("state")) if p)
    image = ((node.get("primary_listing_photo") or {}).get("image") or {}).get("uri")
    return Listing(
        source="facebook",
        source_id=str(listing_id),
        url=f"{BASE}/marketplace/item/{listing_id}/",
        title=str(title).strip(),
        price_cents=dollars_to_cents(price.get("amount")),
        location_text=place,
        image_urls=[image] if image else [],
        pickup=True,  # the search URL asks for local pickup only
    )


def extract_listings(blobs: list) -> list[Listing]:
    found: dict[str, Listing] = {}
    for blob in blobs:
        for node in walk(blob, "marketplace_listing_title"):
            listing = listing_from_node(node)
            if listing and listing.source_id not in found:
                found[listing.source_id] = listing
    return list(found.values())


def apply_detail(listing: Listing, blobs: list) -> bool:
    """Fill in description, photos, coordinates and post time. False if nothing was found."""
    found = False
    for blob in blobs:
        for node in walk(blob, "redacted_description"):
            if str(node.get("id", listing.source_id)) != listing.source_id:
                continue
            found = True
            if node.get("is_sold"):
                raise ListingGone()
            text = (node.get("redacted_description") or {}).get("text")
            if text:
                listing.description = text.strip()
            photos = [
                ((photo or {}).get("image") or {}).get("uri")
                for photo in node.get("listing_photos") or []
            ]
            photos = [uri for uri in photos if uri]
            if photos:
                listing.image_urls = photos
            location = node.get("location") or {}
            if location.get("latitude") is not None and location.get("longitude") is not None:
                listing.lat, listing.lon = float(location["latitude"]), float(location["longitude"])
            if node.get("creation_time"):
                listing.posted_at = datetime.fromtimestamp(
                    int(node["creation_time"]), timezone.utc
                ).isoformat(timespec="seconds")
    return found


def _first_line(exc: Exception) -> str:
    return (str(exc).strip().splitlines() or [type(exc).__name__])[0][:200]


def profile_dir(config: Config):
    path = config.data_dir / "facebook-profile"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    return path


def has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def launch(playwright, config: Config, options: dict, *, headless: bool):
    kwargs = {
        "headless": headless,
        "viewport": {"width": 1280, "height": 900},
        # Keep cookie storage independent of any desktop keyring, so a session saved
        # during fb-login (e.g. over ssh -Y) is readable by the timer's runs.
        "args": ["--password-store=basic"],
    }
    if options.get("browser_channel"):
        kwargs["channel"] = options["browser_channel"]
    return playwright.chromium.launch_persistent_context(str(profile_dir(config)), **kwargs)


def marketplace_location(options: dict) -> str:
    location = str(options.get("location") or "").strip()
    if not location:
        raise SourceSkipped(
            "set `location` under [sources.facebook] in config.toml: the last part of "
            "facebook.com/marketplace/<location>/ once your area is chosen there"
        )
    return location


def interactive_login(config: Config) -> None:
    from playwright.sync_api import sync_playwright

    if not has_display():
        raise SystemExit(
            "fb-login needs somewhere to show a browser window (no DISPLAY set).\n"
            "On a headless machine, connect with X11 forwarding from a computer that has a\n"
            "screen and run it again:  ssh -Y <this-host>"
        )
    options = config.sources["facebook"].options if "facebook" in config.sources else {}
    # The location may not be known yet: finding it out is step 3 below.
    location = str(options.get("location") or "").strip()
    with sync_playwright() as playwright:
        context = launch(playwright, config, options, headless=False)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(f"{BASE}/marketplace/{location}/" if location else f"{BASE}/marketplace/")
        print(
            "\nIn the browser window:\n"
            "  1. Log in to Facebook (approve any verification prompt).\n"
            f"  2. Open Marketplace, click the location, choose {config.area} and a "
            f"{config.radius_miles:g} mile radius.\n"
            "  3. Check the address bar reads facebook.com/marketplace/<location>/ and put that\n"
            "     <location> in config.toml under [sources.facebook] as `location`.\n"
        )
        input("Press Enter here when done; the window will close and the session is kept. ")
        context.close()


class Facebook(Source):
    name = "facebook"
    title_keywords = True
    strict_keywords = True
    delay = (4.0, 9.0)

    def __init__(self, config, source_config):
        super().__init__(config, source_config)
        self.options = source_config.options
        self.max_enrich_per_run = int(self.options.get("max_detail_pages", 6))
        self._playwright = None
        self._context = None
        self._page = None
        self._blobs: list = []

    # -- browser ------------------------------------------------------------

    def _open(self):
        if self._page is not None:
            return self._page
        headless = bool(self.options.get("headless", False))
        if not headless and not has_display():
            raise SourceError("no display available; run under xvfb-run (apt install xvfb)")
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright

        try:
            self._playwright = sync_playwright().start()
            self._context = launch(self._playwright, self.config, self.options, headless=headless)
        except PlaywrightError as exc:
            self.close()
            raise SourceError(
                "could not start the browser; run `uv run playwright install chromium` and "
                f"`uv run playwright install-deps chromium` ({_first_line(exc)})"
            ) from exc
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.on("response", self._on_response)
        return self._page

    def _on_response(self, response) -> None:
        if "/api/graphql" not in response.url:
            return
        try:
            self._blobs.extend(parse_json_blobs(response.text()))
        except Exception:  # body unavailable (navigation, non-text); not worth failing over
            pass

    def _load(self, url: str) -> list:
        """Navigate and return every JSON document the page embedded or fetched."""
        page = self._open()
        self.pause()
        self._blobs = []
        from playwright.sync_api import Error as PlaywrightError

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(random.randint(2500, 4500))
        except PlaywrightError as exc:
            raise SourceError(f"facebook page did not load ({_first_line(exc)})") from exc
        if any(marker in page.url for marker in LOGIN_MARKERS) or page.locator(
            'form input[name="pass"]'
        ).count():
            raise SourceNeedsAttention(NEEDS_LOGIN)
        embedded = page.eval_on_selector_all(
            'script[type="application/json"]', "nodes => nodes.map(n => n.textContent)"
        )
        blobs = []
        for text in embedded:
            blobs.extend(parse_json_blobs(text or ""))
        return blobs

    # -- source interface -----------------------------------------------------

    def search(self, query: str) -> list[Listing]:
        params = {
            "query": query,
            "maxPrice": self.config.max_price,
            "sortBy": "creation_time_descend",
            "daysSinceListed": int(self.options.get("days_since_listed", 7)),
            "deliveryMethod": "local_pick_up",
            "exact": "false",
        }
        location = marketplace_location(self.options)
        blobs = self._load(f"{BASE}/marketplace/{location}/search?{urlencode(params)}")

        for _ in range(int(self.options.get("scrolls", 2))):
            self._page.mouse.wheel(0, random.randint(2500, 4000))
            self._page.wait_for_timeout(random.randint(2000, 3500))
        blobs += self._blobs

        listings = extract_listings(blobs)
        if not listings and "marketplace" not in self._page.url:
            raise SourceError(f"facebook search landed on an unexpected page: {self._page.url}")
        log.info("facebook: %d listings for %r", len(listings), query)
        return listings

    def enrich(self, listing: Listing) -> None:
        blobs = self._load(listing.url)
        blobs += self._blobs
        if not apply_detail(listing, blobs):
            if "/marketplace/item/" not in self._page.url:
                raise ListingGone()  # removed listings redirect to the Marketplace home page
            raise SourceError("facebook listing page had no description data")

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
            if self._playwright is not None:
                self._playwright.stop()
        finally:
            self._context = self._playwright = self._page = None
