from __future__ import annotations

from ..config import Config
from .base import Source

SOURCE_NAMES = ("craigslist", "offerup", "ebay", "facebook")


def build_source(name: str, config: Config) -> Source:
    source_config = config.sources[name]
    if name == "craigslist":
        from .craigslist import Craigslist

        return Craigslist(config, source_config)
    if name == "offerup":
        from .offerup import OfferUp

        return OfferUp(config, source_config)
    if name == "ebay":
        from .ebay import Ebay

        return Ebay(config, source_config)
    if name == "facebook":
        from .facebook import Facebook

        return Facebook(config, source_config)
    raise KeyError(name)


def enabled_sources(config: Config) -> dict[str, Source]:
    return {
        name: build_source(name, config)
        for name in SOURCE_NAMES
        if name in config.sources and config.sources[name].enabled
    }
