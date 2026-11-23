"""Download listing photos, for the evaluator to look at and for Discord posts."""

from __future__ import annotations

import logging
from pathlib import Path

import httpx

from .sources.base import USER_AGENT

log = logging.getLogger(__name__)

MAX_PHOTO_BYTES = 5 * 1024 * 1024


def download_photos(urls: list[str], directory: Path, limit: int) -> list[str]:
    """Save up to `limit` photos into `directory`. Returns the file names written."""
    names = []
    with httpx.Client(
        headers={"User-Agent": USER_AGENT}, timeout=20, follow_redirects=True
    ) as client:
        for url in urls[:limit]:
            try:
                response = client.get(url)
                response.raise_for_status()
            except (httpx.HTTPError, ValueError) as exc:
                log.debug("photo download failed: %s", exc)
                continue
            content_type = response.headers.get("content-type", "")
            if not content_type.startswith("image/") or len(response.content) > MAX_PHOTO_BYTES:
                continue
            suffix = {"image/png": ".png", "image/webp": ".webp", "image/gif": ".gif"}.get(
                content_type.split(";")[0], ".jpg"
            )
            name = f"photo{len(names) + 1}{suffix}"
            (directory / name).write_bytes(response.content)
            names.append(name)
    return names
