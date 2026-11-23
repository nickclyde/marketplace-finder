"""What an install is looking for.

A profile holds everything that is specific to one kind of item: the fields
Claude fills in, the rules code applies to them, the keywords that keep
unrelated listings away from Claude, and how a match is dressed up in a post.

The frame around all of that stays out of a profile's reach. The fence around
seller text lives in `evaluate.py` and `launch.py`, and the escaping and limits
for a post live in `discord.py`, so no profile can leave them out.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..models import Listing
from ..text import flat

if TYPE_CHECKING:
    from ..config import Config

GREY = "#99AAB5"
CONDITION_LABELS = {"yes": "Working, per the seller", "unknown": "Not stated", "no": "Not working"}


class Profile:
    name = ""
    # Starts the title of every private alert, e.g. "CRT finder: craigslist is failing".
    label = "Marketplace finder"
    # Searches run on every source, unless `source_queries` or config.toml says otherwise.
    queries: list[str] = []
    source_queries: dict[str, list[str]] = {}
    # The JSON schema Claude must fill in for a listing.
    schema: dict = {}
    # Finishes "Read each of them before answering: ...".
    photo_hint = "labels and close-ups often show the maker and model when the text does not."
    # Finishes "You are choosing listings for a roundup of ...".
    roundup = "the best-value listings for sale nearby right now"
    # What value is judged on in the opening post, one point each. Distance is always added.
    ranking_criteria: list[str] = []

    def queries_for(self, source: str) -> list[str]:
        return list(self.source_queries.get(source) or self.queries)

    # -- evaluation ---------------------------------------------------------

    def brief(self, config: Config) -> str:
        """Who the listing is being screened for, what they want, and how to answer."""
        raise NotImplementedError

    def passes(self, verdict: dict) -> bool:
        """Apply the criteria to what Claude described. Unknowns pass; explicit failures do not."""
        raise NotImplementedError

    def relevant(self, title: str, description: str, *, strict: bool) -> bool:
        """Whether the text could be describing a wanted item. Runs before Claude is asked.

        `strict` is for sources whose search returns anything loosely related.
        """
        return True

    # -- reference lookups (optional) ---------------------------------------

    def references(self, db, config: Config):
        """Something with `find(*texts, brand=None)` and `close()`, or None."""
        return None

    def reference_query(self, verdict: dict) -> tuple[str, str | None] | None:
        """(model, brand) to look up once Claude has identified the item, or None."""
        return None

    def reference_intro(self, reference: dict) -> str:
        """Our own words on what the reference entry is and how far to rely on it."""
        return ""

    def reference_text(self, reference: dict) -> str:
        """The entry itself. Third-party text: it only ever goes inside the fence."""
        return ""

    # -- the post -----------------------------------------------------------
    # Whatever these return is shown as given, so anything taken from a verdict
    # must go through `text.clean` or `text.plain` first. Titles are the
    # exception: `discord.post_title` cleans those itself.

    def badges(self, verdict: dict) -> list[str]:
        return []

    def colour(self, verdict: dict, flair: list[str]) -> str:
        return GREY

    def title_name(self, verdict: dict) -> str | None:
        """What to call the item in the post's title; None falls back to the listing's title."""
        model, brand = verdict.get("model"), verdict.get("brand")
        if not model:
            return None
        return model if not brand or brand.lower() in model.lower() else f"{brand} {model}"

    def title_extras(self, verdict: dict) -> list[str]:
        """Short facts that follow the name in the title, before the price."""
        return []

    def fields(self, verdict: dict) -> list[tuple[str, str, bool]]:
        """Embed fields as (name, value, inline)."""
        return [("Condition", CONDITION_LABELS.get(verdict.get("working", "unknown"), "Not stated"), True)]

    # -- the opening post ---------------------------------------------------

    def candidate_facts(self, verdict: dict) -> list[str]:
        """What the ranking should know about a listing beyond its name, price and distance."""
        return [f"working {flat(verdict.get('working', 'unknown'))}"]

    def candidate_line(self, listing: Listing, verdict: dict) -> str:
        distance = "distance unknown" if listing.distance_mi is None else f"{listing.distance_mi:.0f} mi"
        parts = [
            listing.key,
            flat(self.title_name(verdict)) or f"model unknown ({flat(listing.title[:60])})",
            listing.price_text,
            distance,
            *self.candidate_facts(verdict),
            flat(verdict.get("summary")),
        ]
        return " | ".join(part for part in parts if part)

    def welcome(self, config: Config) -> str:
        """Posted once, ahead of the top picks, unless config.toml gives its own wording."""
        return (
            f"**{self.label} is live.** This channel now gets a post whenever a matching listing "
            f"turns up within about {config.radius_miles:g} miles of {config.area}, priced under "
            f"${config.max_price}. Details come from the listing, so confirm with the seller "
            "before you go. To start things off, here are the best-value listings up right now."
        )

    # -- testing ------------------------------------------------------------

    def sample(self, config: Config) -> tuple[Listing, dict]:
        """A made-up listing and verdict, for `notify-test`."""
        raise NotImplementedError
