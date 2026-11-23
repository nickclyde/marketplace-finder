from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Listing:
    source: str
    source_id: str
    url: str
    title: str
    price_cents: int | None = None
    location_text: str = ""
    lat: float | None = None
    lon: float | None = None
    distance_mi: float | None = None
    image_urls: list[str] = field(default_factory=list)
    description: str = ""
    pickup: bool | None = None  # None = the source does not say
    posted_at: str = ""
    # Opaque per-source values needed to fetch the detail later (never shown to Claude).
    extra: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    @property
    def price_text(self) -> str:
        if self.price_cents is None:
            return "price not stated"
        dollars, cents = divmod(self.price_cents, 100)
        return f"${dollars:,}" if cents == 0 else f"${dollars:,}.{cents:02d}"
