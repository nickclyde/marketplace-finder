from __future__ import annotations

import html
import random
import re
import time

import httpx

from ..config import Config, SourceConfig
from ..models import Listing

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)


class SourceError(Exception):
    """The source failed this run (bad response, changed format, network)."""


class SourceBlocked(SourceError):
    """The site refused the request (403/429 or a challenge page). Do not retry this run."""


class SourceNeedsAttention(SourceError):
    """A person has to do something (for example, log in again) before this source can work."""


class SourceSkipped(Exception):
    """The source is not set up yet; not a failure."""


class ListingGone(Exception):
    """The listing was removed or sold between search and detail fetch."""


class Source:
    name = ""
    # True when the site's search is fuzzy enough that titles must be keyword-checked
    # before spending a detail fetch on them.
    title_keywords = False
    # True when the site returns anything by the brand, so the keyword check must ask
    # for a TV term or a model family as well.
    strict_keywords = False
    # Seconds to wait between consecutive requests to the same site.
    delay = (2.0, 5.0)
    # Listing pages fetched per run; the rest wait for the next run.
    max_enrich_per_run = 40

    def __init__(self, config: Config, source_config: SourceConfig):
        self.config = config
        self.source_config = source_config
        self._last_request = 0.0

    def search(self, query: str) -> list[Listing]:
        raise NotImplementedError

    def enrich(self, listing: Listing) -> None:
        """Fill in description, photos and anything else only the listing page has."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def pause(self) -> None:
        low, high = self.delay
        if high <= 0:
            return
        wait = self._last_request + random.uniform(low, high) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()


def http_client(**kwargs) -> httpx.Client:
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    return httpx.Client(headers=headers, timeout=30, follow_redirects=True, **kwargs)


def raise_for_block(response: httpx.Response) -> None:
    if response.status_code in (403, 429):
        raise SourceBlocked(f"HTTP {response.status_code} from {response.request.url.host}")
    if response.status_code >= 400:
        raise SourceError(f"HTTP {response.status_code} from {response.request.url.host}")


def html_to_text(fragment: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>", "\n", fragment)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def dollars_to_cents(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(round(float(str(value).replace("$", "").replace(",", "")) * 100))
    except ValueError:
        return None
