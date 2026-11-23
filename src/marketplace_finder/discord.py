"""Post listings to a Discord channel, through a channel webhook or the `discli` command.

Titles, summaries and reference text all trace back to strangers' listings, so
they are only ever placed inside the embed, where a mention does not ping
anyone, and never in the message content. With discli everything is handed over
as separate `--option=value` arguments with no shell involved; with a webhook
every message is sent with mentions switched off.

What a post says about the item (its flair, colour and fields) comes from the
profile. The escaping, the limits and the layout are decided here.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

from .config import Config
from .models import Listing
from .photos import download_photos
from .text import clean, clip, plain  # noqa: F401  (plain is part of this module's surface)

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 90
PAUSE_BETWEEN_POSTS = 1.0
# discli gives up on a rate limit instead of waiting; it says how long Discord asked for.
RATE_LIMITED = re.compile(r"Rate limited\. Retry after ([\d.]+)s")
MAX_RATE_LIMIT_WAIT = 120.0
RATE_LIMIT_RETRIES = 2

# Discord's limits for an embed.
MAX_TITLE, MAX_DESCRIPTION, MAX_FIELD, MAX_AUTHOR = 256, 4096, 1024, 256
# A post is read in a chat channel, so the list of things to check stays short.
MAX_CONCERNS, MAX_CONCERN_CHARS = 4, 140

# What a channel webhook's address looks like. Nothing is ever posted anywhere else.
WEBHOOK_URL = re.compile(
    r"^https://(?:(?:canary|ptb)\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/[\w-]+$"
)


class DiscordError(Exception):
    pass


def post_title(listing: Listing, verdict: dict, config: Config) -> str:
    profile = config.profile
    name = profile.title_name(verdict) or listing.title
    parts = [clean(" ".join(name.split())[:80])]
    parts += profile.title_extras(verdict)
    parts.append(listing.price_text)
    if listing.location_text:
        parts.append(clean(" ".join(listing.location_text.split())[:60]))
    return clip(" · ".join(parts), MAX_TITLE)


def discord_post(
    listing: Listing,
    verdict: dict,
    config: Config,
    *,
    rank: tuple[int, int] | None = None,
    reason: str | None = None,
) -> dict:
    """The embed for a listing that passed, as plain data.

    `rank` and `reason` are for the opening post's picks: (position, of how many)
    and why it was chosen.
    """
    profile = config.profile
    flair = profile.badges(verdict)
    author = ([f"#{rank[0]} of {rank[1]}"] if rank else []) + flair

    lines = [clean(reason)] if reason else []
    lines.append(clean(verdict.get("summary", "")))
    concerns = [clean(clip(str(point), MAX_CONCERN_CHARS)) for point in verdict.get("concerns") or []]
    if concerns:
        lines.append("**Confirm:** " + "; ".join(concerns[:MAX_CONCERNS]))

    footer = listing.source
    if listing.distance_mi is not None:
        about = "about " if listing.extra.get("distance_approx") else ""
        footer += f" · {about}{listing.distance_mi:.0f} mi from {config.area}"

    return {
        "author": clip(" · ".join(author), MAX_AUTHOR),
        "title": post_title(listing, verdict, config),
        "url": listing.url,
        "description": clip("\n".join(line for line in lines if line), MAX_DESCRIPTION),
        "fields": [
            (name, clip(value, MAX_FIELD), inline) for name, value, inline in profile.fields(verdict)
        ],
        "footer": footer,
        "color": profile.colour(verdict, flair),
    }


def embed_args(channel: str, post: dict, image: Path | None = None) -> list[str]:
    """Arguments for `discli message embed`. Values ride on their option, so none can become one."""
    args = [
        "message",
        "embed",
        channel,
        f"--title={post['title']}",
        f"--url={post['url']}",
        f"--color={post['color']}",
        f"--footer={post['footer']}",
    ]
    if post["description"]:
        args.append(f"--description={post['description']}")
    if post["author"]:
        args.append(f"--author={post['author']}")
    for name, value, inline in post["fields"]:
        args.append(f"--field={name}|{value}" + ("|inline" if inline else ""))
    if image is not None:
        args.append(f"--image={image}")
    return args


def embed_payload(post: dict, image_name: str | None = None) -> dict:
    """The same post as the JSON a webhook takes. Seller text stays inside the embed."""
    embed: dict = {
        "title": post["title"],
        "url": post["url"],
        "color": int(post["color"].lstrip("#"), 16),
        "footer": {"text": post["footer"]},
        "fields": [
            {"name": name, "value": value, "inline": inline} for name, value, inline in post["fields"]
        ],
    }
    if post["description"]:
        embed["description"] = post["description"]
    if post["author"]:
        embed["author"] = {"name": post["author"]}
    if image_name:
        embed["image"] = {"url": f"attachment://{image_name}"}
    return {"embeds": [embed]}


def find_discli() -> str:
    path = shutil.which("discli")
    if path:
        return path
    fallback = Path.home() / ".local" / "bin" / "discli"
    if fallback.exists():
        return str(fallback)
    raise DiscordError("the discli command was not found on PATH or in ~/.local/bin")


class Poster:
    """What both ways of posting share: building the post and fetching its photo."""

    config: Config
    # Where posts go, in words for the operator.
    target = ""
    # False when pointed somewhere other than the configured channel, for testing.
    is_main = True

    @property
    def configured(self) -> bool:
        raise NotImplementedError

    def _embed(self, post: dict, image: Path | None) -> bool:
        raise NotImplementedError

    def send_text(self, text: str) -> bool:
        """A plain message. Only for wording that comes from config.toml or the profile."""
        raise NotImplementedError

    def _ready(self) -> bool:
        if not self.configured:
            log.warning("Discord posting is not set up (see [discord] in config.toml); nothing sent")
        return self.configured

    def send_match(
        self,
        listing: Listing,
        verdict: dict,
        *,
        rank: tuple[int, int] | None = None,
        reason: str | None = None,
    ) -> bool:
        if not self._ready():
            return False
        post = discord_post(listing, verdict, self.config, rank=rank, reason=reason)
        with tempfile.TemporaryDirectory(prefix="marketplace-post-") as tmp:
            # The photo is uploaded rather than linked: marketplace image links expire.
            # Only this downloaded copy is ever uploaded, never a path or URL from a listing.
            photos = download_photos(listing.image_urls, Path(tmp), 1) if listing.image_urls else []
            image = Path(tmp) / photos[0] if photos else None
            return self._embed(post, image)


class DiscordNotifier(Poster):
    """Posts with `discli`, as a bot, to a channel named in config.toml."""

    def __init__(
        self,
        config: Config,
        *,
        channel: str | None = None,
        command: str | None = None,
        pause=time.sleep,
    ):
        self.config = config
        self.channel = channel or config.discord_channel
        self.command = command
        self.pause = pause
        self.target = f"#{self.channel}"
        self.is_main = self.channel == config.discord_channel

    @property
    def configured(self) -> bool:
        return bool(self.config.discord_enabled and self.channel)

    def _run(self, args: list[str]) -> bool:
        if not self._ready():
            return False
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                result = subprocess.run(
                    [self.command or find_discli(), *args],
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT_SECONDS,
                )
            except (DiscordError, OSError, subprocess.TimeoutExpired) as exc:
                log.error("discli could not be run: %s", exc)
                return False
            if result.returncode == 0:
                self.pause(PAUSE_BETWEEN_POSTS)  # Discord allows five messages in five seconds
                return True
            detail = (result.stderr or result.stdout).strip()[:300]
            limited = RATE_LIMITED.search(detail)
            wait = float(limited.group(1)) + 1 if limited else None
            if wait is None or wait > MAX_RATE_LIMIT_WAIT or attempt == RATE_LIMIT_RETRIES:
                log.error("discli exited %d: %s", result.returncode, detail)
                return False
            # Nothing was posted, so sending the same message again cannot duplicate it.
            log.info("Discord rate limit; waiting %.0f seconds before trying again", wait)
            self.pause(wait)
        return False

    def _embed(self, post: dict, image: Path | None) -> bool:
        return self._run(embed_args(self.channel, post, image))

    def send_text(self, text: str) -> bool:
        return self._run(["message", "send", self.channel, "--", text])


class WebhookNotifier(Poster):
    """Posts through a channel webhook: one address in .env, no bot and nothing to install.

    The address is a credential (anyone holding it can post to the channel), so
    it never appears in a log line.
    """

    def __init__(self, config: Config, *, test: bool = False, client=None, pause=time.sleep):
        self.config = config
        self.url = config.discord_test_webhook_url if test else config.discord_webhook_url
        self.client = client or httpx
        self.pause = pause
        self.target = "the test webhook's channel" if test else "the webhook's channel"
        self.is_main = not test

    @property
    def configured(self) -> bool:
        return bool(self.config.discord_enabled and self.url)

    def _redact(self, text: str) -> str:
        return text.replace(self.url, "<webhook>").replace(self.url.rsplit("/", 1)[-1], "<token>")

    def _post(self, payload: dict, image: Path | None = None) -> bool:
        if not self._ready():
            return False
        if not WEBHOOK_URL.match(self.url):
            log.error("the webhook address in .env does not look like a Discord webhook; nothing sent")
            return False
        # No mention in any message can ping anyone, whatever the text says.
        payload = {**payload, "allowed_mentions": {"parse": []}}
        files = {"payload_json": (None, json.dumps(payload), "application/json")}
        if image is not None:
            kind = mimetypes.guess_type(image.name)[0] or "application/octet-stream"
            files["files[0]"] = (image.name, image.read_bytes(), kind)
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                response = self.client.post(
                    self.url, params={"wait": "true"}, files=files, timeout=TIMEOUT_SECONDS
                )
            except httpx.HTTPError as exc:
                # Only the kind of error: the message of some httpx errors quotes the address.
                log.error("the Discord webhook could not be reached: %s", type(exc).__name__)
                return False
            if response.status_code < 300:
                self.pause(PAUSE_BETWEEN_POSTS)
                return True
            detail = self._redact(response.text.strip())[:300]
            wait = self._retry_after(response) if response.status_code == 429 else None
            if wait is None or wait > MAX_RATE_LIMIT_WAIT or attempt == RATE_LIMIT_RETRIES:
                log.error("the Discord webhook answered %d: %s", response.status_code, detail)
                return False
            # A 429 means nothing was posted, so sending again cannot duplicate it.
            log.info("Discord rate limit; waiting %.0f seconds before trying again", wait)
            self.pause(wait)
        return False

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        # The header is always in seconds; older API versions give the body's figure in ms.
        try:
            return float(response.headers["retry-after"]) + 1
        except (ValueError, KeyError):
            pass
        try:
            return float(response.json()["retry_after"]) + 1
        except (ValueError, KeyError, TypeError):
            return None

    def _embed(self, post: dict, image: Path | None) -> bool:
        return self._post(embed_payload(post, image.name if image else None), image)

    def send_text(self, text: str) -> bool:
        return self._post({"content": text})


def build_notifier(config: Config, *, channel: str | None = None, test: bool = False) -> Poster:
    """The poster config.toml asks for. `channel` and `test` point it somewhere else for a trial."""
    if config.discord_via == "webhook":
        if channel:
            raise DiscordError(
                "--channel does not apply when posting through a webhook, which is tied to one "
                "channel. Put a second webhook in .env as DISCORD_TEST_WEBHOOK_URL and use --test."
            )
        if test and not config.discord_test_webhook_url:
            raise DiscordError("--test needs DISCORD_TEST_WEBHOOK_URL in .env")
        return WebhookNotifier(config, test=test)
    if test:
        raise DiscordError("--test is for webhook posting. With discli, name a channel with --channel.")
    return DiscordNotifier(config, channel=channel)
