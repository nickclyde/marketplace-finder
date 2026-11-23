"""Model reference entries from crtdatabase.com.

Three kinds of page are fetched: the list of brands, a brand's index (the
models the site knows for it) and individual model pages. The site's robots.txt
disallows /search and asks for three seconds between requests, so models are
found through the indexes rather than the search, and requests are spaced
accordingly. Every page is cached in the database, so each is fetched rarely.

Entry text is licensed CC BY-NC-SA 4.0; notifications link back to the entry.
"""

from __future__ import annotations

import json
import logging
import re
import time
from functools import lru_cache
from datetime import datetime, timedelta, timezone

import httpx

from .db import Database
from .sources.base import html_to_text

log = logging.getLogger(__name__)

BASE = "https://crtdatabase.com"
BRANDS_PATH = "/crts"
CLIENT_NAME = "marketplace-finder/0.1"
CRAWL_DELAY = 3.0
INDEX_MAX_AGE = timedelta(days=7)
ENTRY_MAX_AGE = timedelta(days=30)
PAUSE_WHEN_BLOCKED = timedelta(hours=24)
HEALTH_NAME = "crtdatabase"

BRANDS_KEY = "crtdb:brands"
INDEX_KEY = "crtdb:index:"  # + brand slug
ENTRY_KEY = "crtdb:entry:"  # + brand slug / model slug

# Brands looked up for one listing when the text names several.
MAX_BRANDS = 2
# Listings often name the Sony family without the brand.
SONY_HINTS = re.compile(r"\b(trinitron|wega|[pb]vm|kv[- ]?\d{2})", re.IGNORECASE)
# Brands on the site whose names are also everyday words, first names or places. In
# listing text they say nothing ("sharp picture", "Apple TV", "Portland"), so they
# are only looked up when the evaluator names the brand.
AMBIGUOUS_BRANDS = frozenset({
    "alt", "apex", "apple", "astra", "candle", "capri", "citizen", "elsa", "esa", "fisher",
    "ilo", "mag", "pioneer", "portland", "prima", "princeton", "proton", "radius", "ric",
    "sears", "sharp", "tera", "thomas",
})
# What people call a brand, where it differs from the site's name for it.
BRAND_ALIASES = {
    "ge": "general-electric",
    "sgi": "silicon-graphics-inc-sgi",
    "silicongraphics": "silicon-graphics-inc-sgi",
    "orion": "orion-japan",
    "sun": "sun-microsystems",
    "bo": "bang-olufsen",
}

# Anything shaped like a model number: 2-4 letters, an optional separator, then
# digits and letters, possibly after a letter or two ("KV-27FS120", "kv 20m20",
# "PVM14M2U", "BVM-D14H5U"). Only candidates the index knows are used, so loose
# matching here costs nothing.
CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z]{2,4})[- ]?([A-Za-z]{0,2}\d[A-Za-z0-9]*(?:-[A-Za-z0-9]+)*)"
)
MIN_PREFIX = 6

# Specification rows worth showing to the evaluator, in this order.
PROMPT_SPECS = (
    "Model",
    "Series",
    "Viewable Size",
    "Input Signals",
    "Mods",
    "Native Resolutions",
    "Formats",
    "Aspect",
    "Tube",
    "Chassis",
    "Application",
    "Launched",
    "Weight",
    "Speakers",
    "Remote",
)


def normalise(model: str) -> str:
    return re.sub(r"[^a-z0-9]", "", model.lower())


def candidates(text: str) -> list[str]:
    """Normalised model-number candidates, in the order they appear."""
    found = []
    for match in CANDIDATE.finditer(text or ""):
        key = normalise(match.group(1) + match.group(2))
        if key not in found:
            found.append(key)
    return found


def parse_brands(page: str) -> list[str]:
    """Brand slugs from the page that lists every brand."""
    slugs = []
    for slug in re.findall(r'href="' + re.escape(BRANDS_PATH) + r'/([a-z0-9][a-z0-9-]*)"', page):
        if slug not in slugs:
            slugs.append(slug)
    return slugs


def parse_index(page: str, brand: str) -> dict[str, str]:
    """Map normalised model numbers to page slugs from a brand's index."""
    index = {}
    path = re.escape(f"{BRANDS_PATH}/{brand}")
    for slug in re.findall(r'href="' + path + r'/([a-z0-9][a-z0-9-]*)"', page):
        key = normalise(slug.removeprefix(brand + "-"))
        if key:
            index.setdefault(key, slug)
    return index


@lru_cache(maxsize=None)
def brand_pattern(slug: str) -> re.Pattern:
    words = r"[\s&-]*".join(re.escape(word) for word in slug.split("-"))
    return re.compile(r"(?<![a-z0-9])" + words + r"(?![a-z0-9])", re.IGNORECASE)


def brands_in_text(text: str, brands: list[str]) -> list[str]:
    """Brand slugs named in the text, in the order they appear."""
    found = []
    for slug in brands:
        if slug in AMBIGUOUS_BRANDS:
            continue
        match = brand_pattern(slug).search(text)
        if match:
            found.append((match.start(), slug))
    hint = SONY_HINTS.search(text)
    if hint and "sony" in brands and all(slug != "sony" for _, slug in found):
        found.append((hint.start(), "sony"))
    return [slug for _, slug in sorted(found)]


def brand_slug(name: str, brands: list[str]) -> str | None:
    """The site's slug for a brand as the evaluator wrote it, or None."""
    key = normalise(name)
    if not key:
        return None
    for slug in brands:
        if normalise(slug) == key:
            return slug
    if BRAND_ALIASES.get(key) in brands:
        return BRAND_ALIASES[key]
    named = [slug for slug in brands if brand_pattern(slug).search(name)]
    return named[0] if len(named) == 1 else None


def _section(page: str, anchor: str) -> str:
    match = re.search(
        r'<h1 id="' + anchor + r'"[^>]*>.*?</h1>(.*?)(?=<h1|<div|</body|$)', page, re.S
    )
    return html_to_text(match.group(1)) if match else ""


def parse_entry(page: str, brand: str, slug: str) -> dict:
    specs = {}
    table = re.search(r'<table id="specifications-list".*?</table>', page, re.S)
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(0) if table else "", re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) != 2:
            continue
        label, value = (re.sub(r"\s+", " ", html_to_text(cell)).strip() for cell in cells)
        label = label.rstrip(":").strip()
        value = re.sub(r"\s+,", ",", value)
        if label and value:
            specs[label] = value
    summary = _section(page, "summary")
    notes = _section(page, "notes")
    if not specs and not summary and not notes:
        raise ValueError("no summary, notes or specifications found")
    model = specs.get("Model") or slug.removeprefix(brand + "-").upper()
    brand_name = specs.get("Brand") or brand.replace("-", " ").title()
    return {
        "model": model,
        "name": f"{brand_name} {model}",
        "url": f"{BASE}{BRANDS_PATH}/{brand}/{slug}",
        "summary": summary,
        "notes": notes,
        "specs": specs,
    }


def prompt_text(entry: dict) -> str:
    """The entry as plain text for the evaluator."""
    specs = entry.get("specs", {})
    lines = [f"{label}: {specs[label]}" for label in PROMPT_SPECS if specs.get(label)]
    if entry.get("summary"):
        lines += ["", "Summary:", entry["summary"]]
    if entry.get("notes"):
        lines += ["", "Notes:", entry["notes"]]
    return "\n".join(lines)


class Blocked(Exception):
    pass


class CrtDatabase:
    def __init__(self, db: Database, client: httpx.Client | None = None, sleep=time.sleep):
        self.db = db
        self.client = client or httpx.Client(
            headers={"User-Agent": CLIENT_NAME}, timeout=30, follow_redirects=True
        )
        self.sleep = sleep
        self._last_request: float | None = None
        self._brands: list[str] | None = None
        self._indexes: dict[str, dict[str, str]] = {}
        self._off = False  # set after a failure, to leave the site alone for this run

    def close(self) -> None:
        self.client.close()

    # -- public -------------------------------------------------------------

    def find(self, *texts: str, brand: str | None = None) -> dict | None:
        """The entry for the first known model named in the texts, or None.

        The brand comes from `brand` when the evaluator has named one, otherwise
        from the texts; without a brand there is no index to look in.

        Never raises: a lookup is a nice-to-have and must not hold up an evaluation.
        """
        try:
            brands = self._load_brands()
            wanted = []
            named = brand_slug(brand, brands) if brand else None
            if named:
                wanted.append(named)
            for slug in brands_in_text("\n".join(text or "" for text in texts), brands):
                if slug not in wanted and len(wanted) < MAX_BRANDS:
                    wanted.append(slug)
            for slug in wanted:
                entry = self._find_in(slug, texts)
                if entry:
                    return entry
        except Exception as exc:  # network, parsing, anything
            log.warning("crtdatabase lookup failed: %s: %s", type(exc).__name__, exc)
        return None

    def _find_in(self, brand: str, texts: tuple[str, ...]) -> dict | None:
        index = self._load_index(brand)
        if not index:
            return None
        prefix = normalise(brand)
        for text in texts:
            for key in candidates(text):
                slug, how = self._match(key, index)
                if not slug and key.startswith(prefix):
                    # "JVC 7860" reads as one candidate; the index has it without the brand.
                    slug, how = self._match(key[len(prefix):], index)
                if slug:
                    entry = self._load_entry(brand, slug)
                    if entry:
                        return {**entry, "matched": how}
        return None

    # -- matching -----------------------------------------------------------

    @staticmethod
    def _match(key: str, index: dict[str, str]) -> tuple[str | None, str]:
        if key in index:
            return index[key], "exact"
        if len(key) >= MIN_PREFIX:
            longer = [slug for known, slug in index.items() if known.startswith(key)]
            if len(longer) == 1:
                return longer[0], "closest"
        return None, ""

    # -- cache and network ----------------------------------------------------

    def _fresh(self, cached: tuple[str, str] | None, max_age: timedelta) -> bool:
        if cached is None:
            return False
        return datetime.now(timezone.utc) - datetime.fromisoformat(cached[1]) < max_age

    def _cached_page(self, key: str, path: str, parse, what: str):
        """A parsed page from the cache, refetched when stale; the old copy if that fails."""
        cached = self.db.cache_get(key)
        if not self._fresh(cached, INDEX_MAX_AGE):
            page = self._get(path)
            if page is not None:
                parsed = parse(page)
                if parsed:
                    self.db.cache_put(key, json.dumps(parsed))
                    return parsed
                log.warning("crtdatabase %s had no links", what)
        return json.loads(cached[0]) if cached else None

    def _load_brands(self) -> list[str]:
        if self._brands is None:
            self._brands = self._cached_page(BRANDS_KEY, BRANDS_PATH, parse_brands, "brand list") or []
        return self._brands

    def _load_index(self, brand: str) -> dict[str, str]:
        if brand not in self._indexes:
            self._indexes[brand] = (
                self._cached_page(
                    INDEX_KEY + brand,
                    f"{BRANDS_PATH}/{brand}",
                    lambda page: parse_index(page, brand),
                    f"index for {brand}",
                )
                or {}
            )
        return self._indexes[brand]

    def _load_entry(self, brand: str, slug: str) -> dict | None:
        key = f"{ENTRY_KEY}{brand}/{slug}"
        cached = self.db.cache_get(key)
        if not self._fresh(cached, ENTRY_MAX_AGE):
            page = self._get(f"{BRANDS_PATH}/{brand}/{slug}")
            if page is not None:
                try:
                    entry = parse_entry(page, brand, slug)
                except ValueError as exc:
                    log.warning("crtdatabase entry %s could not be read: %s", slug, exc)
                else:
                    self.db.cache_put(key, json.dumps(entry))
                    return entry
        return json.loads(cached[0]) if cached else None

    def _get(self, path: str) -> str | None:
        """Fetch a page, or None when the site should be left alone."""
        if self._off:
            return None
        paused_until = self.db.health(HEALTH_NAME)["paused_until"]
        if paused_until and paused_until > datetime.now(timezone.utc).isoformat(timespec="seconds"):
            self._off = True
            return None
        if self._last_request is not None:
            wait = self._last_request + CRAWL_DELAY - time.monotonic()
            if wait > 0:
                self.sleep(wait)
        self._last_request = time.monotonic()
        try:
            response = self.client.get(BASE + path)
            if response.status_code in (403, 429):
                raise Blocked(f"HTTP {response.status_code}")
            if response.status_code == 404:
                return None
            response.raise_for_status()
        except Blocked as exc:
            self._off = True
            until = datetime.now(timezone.utc) + PAUSE_WHEN_BLOCKED
            self.db.update_health(
                HEALTH_NAME,
                failures=self.db.health(HEALTH_NAME)["failures"] + 1,
                last_error=f"{exc} from crtdatabase.com",
                paused_until=until.isoformat(timespec="seconds"),
            )
            log.warning("crtdatabase.com refused the request (%s); pausing lookups for 24 hours", exc)
            return None
        except httpx.HTTPError as exc:
            self._off = True
            log.warning("crtdatabase.com request failed: %s", exc)
            return None
        if self.db.health(HEALTH_NAME)["failures"]:
            self.db.update_health(HEALTH_NAME, failures=0, last_error=None, paused_until=None)
        return response.text
