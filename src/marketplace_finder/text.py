"""Making text from listings safe to show.

Titles, summaries and reference text all trace back to strangers. These helpers
are the only way that text reaches a Discord post or a ranking prompt.
"""

from __future__ import annotations

import re


def clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rsplit(" ", 1)[0].rstrip(".,;: \n") + "…"


def clean(text: str) -> str:
    """Make text from a listing inert: no markdown, no links, nothing discli unescapes."""
    text = (text or "").replace("\\", "/")
    # Until none is left: taking one out of "hthttp://tp://" would leave another behind.
    while re.search(r"https?://", text, flags=re.IGNORECASE):
        text = re.sub(r"https?://", "", text, flags=re.IGNORECASE)
    return re.sub(r"([*_~`|>\[\]#])", r"\\\1", text).strip()


def plain(text: str) -> str:
    """For field values: discli splits a field on "|", so none may survive."""
    return clean(text).replace("\\|", "/")


def flat(text) -> str:
    """For the ranking prompt: one listing stays on one line, with no separators of its own."""
    return " ".join(str(text or "").split()).replace("|", "/")
