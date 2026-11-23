"""Posting through a channel webhook: same post, no bot, and an address that stays secret."""

import json
import logging

import httpx
import pytest

from marketplace_finder import discord
from marketplace_finder.discord import (
    DiscordError,
    DiscordNotifier,
    WebhookNotifier,
    build_notifier,
    discord_post,
    embed_payload,
)

URL = "https://discord.com/api/webhooks/123456789012345678/s3cret-Token_abc"
TEST_URL = "https://discord.com/api/webhooks/987654321098765432/other-Token_xyz"
VERDICT = {
    "is_crt_display": True, "kind": "tv", "brand": "Sony", "model_number": "KV-27FS120",
    "is_trinitron": True, "screen_size_inches": 27, "working": "unknown",
    "best_input": "component", "input_evidence": "model_knowledge", "rgb_moddable": "no",
    "summary": "27 inch flat-tube Trinitron.", "concerns": ["remote included?"],
}


class FakeDiscordApi:
    """Stands in for httpx: records each request and answers from a queue."""

    def __init__(self, *responses):
        self.responses = list(responses) or [httpx.Response(200, json={"id": "1"})]
        self.requests = []

    def post(self, url, *, params, files, timeout):
        self.requests.append({"url": url, "params": params, "files": files})
        response = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(response, Exception):
            raise response
        return response

    def payload(self, index=-1):
        return json.loads(self.requests[index]["files"]["payload_json"][1])


@pytest.fixture
def webhook_config(config):
    config.discord_via = "webhook"
    config.discord_webhook_url = URL
    return config


def sender(config, api, pauses=None, **kwargs):
    pauses = [] if pauses is None else pauses
    return WebhookNotifier(config, client=api, pause=pauses.append, **kwargs)


def test_embed_payload_carries_the_whole_post(config, make_listing):
    listing = make_listing()
    listing.distance_mi = 16.4
    post = discord_post(listing, VERDICT, config, rank=(1, 3), reason="Cheap.")
    embed = embed_payload(post, "photo1.jpg")["embeds"][0]
    assert embed["title"] == 'Sony KV-27FS120 · 27" · $150 · Roseville, CA'
    assert embed["url"] == listing.url and embed["color"] == 0xE8B923
    assert embed["author"] == {"name": "#1 of 3 · ⭐ Trinitron"}
    assert embed["description"].splitlines()[0] == "Cheap."
    assert embed["footer"] == {"text": "craigslist · 16 mi from Sacramento"}
    assert {"name": "Condition", "value": "Not stated", "inline": True} in embed["fields"]
    assert embed["image"] == {"url": "attachment://photo1.jpg"}
    assert "image" not in embed_payload(post)["embeds"][0]


def test_post_goes_to_the_webhook_with_mentions_switched_off(webhook_config, make_listing):
    api, pauses = FakeDiscordApi(), []
    listing = make_listing(title="@everyone free tv <@&123>")
    assert sender(webhook_config, api, pauses).send_match(listing, {**VERDICT, "model_number": None})
    request = api.requests[0]
    assert request["url"] == URL and request["params"] == {"wait": "true"}
    payload = api.payload()
    assert payload["allowed_mentions"] == {"parse": []}
    # Seller text is inside the embed; the message itself has no content to ping with.
    assert "content" not in payload
    assert payload["embeds"][0]["title"].startswith("@everyone free tv")
    assert pauses == [discord.PAUSE_BETWEEN_POSTS]


def test_photo_is_uploaded_from_a_local_copy(webhook_config, make_listing, monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr(
        "marketplace_finder.photos.httpx.Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, content=b"jpeg bytes", headers={"content-type": "image/jpeg"})
            )
        ),
    )
    api = FakeDiscordApi()
    listing = make_listing(image_urls=["https://img.example/1.jpg"])
    assert sender(webhook_config, api).send_match(listing, VERDICT)
    name, content, kind = api.requests[0]["files"]["files[0]"]
    assert (name, content, kind) == ("photo1.jpg", b"jpeg bytes", "image/jpeg")
    assert api.payload()["embeds"][0]["image"] == {"url": "attachment://photo1.jpg"}
    assert "img.example" not in json.dumps(api.payload())


def test_welcome_text_is_sent_as_content(webhook_config):
    api = FakeDiscordApi()
    assert sender(webhook_config, api).send_text("**Hunting is live.** @everyone")
    assert api.payload() == {
        "content": "**Hunting is live.** @everyone", "allowed_mentions": {"parse": []},
    }


def test_rate_limit_is_waited_out_and_the_post_sent_again(webhook_config, make_listing):
    limited = httpx.Response(429, json={"message": "You are being rate limited.", "retry_after": 2.5})
    api, pauses = FakeDiscordApi(limited, httpx.Response(204)), []
    assert sender(webhook_config, api, pauses).send_match(make_listing(), VERDICT)
    assert pauses == [pytest.approx(3.5), discord.PAUSE_BETWEEN_POSTS]
    assert len(api.requests) == 2


def test_rate_limit_that_does_not_clear_is_a_failure(webhook_config, make_listing):
    limited = httpx.Response(429, json={"retry_after": 1})
    api, pauses = FakeDiscordApi(limited), []
    assert sender(webhook_config, api, pauses).send_match(make_listing(), VERDICT) is False
    assert len(api.requests) == discord.RATE_LIMIT_RETRIES + 1
    assert len(pauses) == discord.RATE_LIMIT_RETRIES


def test_a_very_long_rate_limit_is_left_for_the_next_run(webhook_config, make_listing):
    api, pauses = FakeDiscordApi(httpx.Response(429, headers={"retry-after": "900"})), []
    assert sender(webhook_config, api, pauses).send_match(make_listing(), VERDICT) is False
    assert pauses == [] and len(api.requests) == 1


@pytest.mark.parametrize("failure", [
    httpx.Response(404, json={"message": f"Unknown Webhook {URL}", "code": 10015}),
    httpx.Response(400, text=f"bad request to {URL}"),
    httpx.ConnectError(f"could not connect to {URL}"),
    httpx.ReadTimeout(f"timed out reading {URL}"),
])
def test_the_webhook_address_never_reaches_the_log(webhook_config, make_listing, caplog, failure):
    caplog.set_level(logging.DEBUG)
    api = FakeDiscordApi(failure)
    assert sender(webhook_config, api).send_match(make_listing(), VERDICT) is False
    assert caplog.text  # the failure is reported
    assert "s3cret-Token_abc" not in caplog.text and URL not in caplog.text


@pytest.mark.parametrize("address", [
    "https://example.org/api/webhooks/1/abc",
    "http://discord.com/api/webhooks/1/abc",
    "https://discord.com.evil.example/api/webhooks/1/abc",
    "https://discord.com/api/webhooks/1/abc?thread_id=2",
    "not a url",
])
def test_nothing_is_posted_to_an_address_that_is_not_a_discord_webhook(
    webhook_config, make_listing, address, caplog
):
    webhook_config.discord_webhook_url = address
    api = FakeDiscordApi()
    assert sender(webhook_config, api).send_match(make_listing(), VERDICT) is False
    assert api.requests == [] and address not in caplog.text


def test_nothing_is_sent_when_discord_is_off_or_no_address_is_set(webhook_config, make_listing):
    api = FakeDiscordApi()
    webhook_config.discord_enabled = False
    assert sender(webhook_config, api).send_match(make_listing(), VERDICT) is False
    webhook_config.discord_enabled, webhook_config.discord_webhook_url = True, ""
    assert not sender(webhook_config, api).configured
    assert sender(webhook_config, api).send_text("hello") is False
    assert api.requests == []


def test_config_picks_the_way_of_posting(webhook_config):
    poster = build_notifier(webhook_config)
    assert isinstance(poster, WebhookNotifier) and poster.is_main and poster.url == URL
    # A webhook belongs to one channel, so another channel cannot be named.
    with pytest.raises(DiscordError, match="--test"):
        build_notifier(webhook_config, channel="testing")
    with pytest.raises(DiscordError, match="DISCORD_TEST_WEBHOOK_URL"):
        build_notifier(webhook_config, test=True)
    webhook_config.discord_test_webhook_url = TEST_URL
    trial = build_notifier(webhook_config, test=True)
    assert trial.url == TEST_URL and not trial.is_main


def test_discli_stays_available(config):
    poster = build_notifier(config)
    assert isinstance(poster, DiscordNotifier) and poster.is_main and poster.target == "#listings"
    trial = build_notifier(config, channel="testing")
    assert trial.target == "#testing" and not trial.is_main
    with pytest.raises(DiscordError, match="--channel"):
        build_notifier(config, test=True)


def test_the_address_is_redacted_before_the_log_line_is_cut(webhook_config, make_listing, caplog):
    body = "x" * 280 + " " + URL
    api = FakeDiscordApi(httpx.Response(400, text=body))
    assert sender(webhook_config, api).send_match(make_listing(), VERDICT) is False
    assert "123456789012345678" not in caplog.text and "s3cret" not in caplog.text


def test_retry_after_header_is_trusted_over_the_body(webhook_config, make_listing):
    # Older API versions give the body's figure in milliseconds; the header is always seconds.
    limited = httpx.Response(429, headers={"retry-after": "3"}, json={"retry_after": 3000})
    api, pauses = FakeDiscordApi(limited, httpx.Response(204)), []
    assert sender(webhook_config, api, pauses).send_match(make_listing(), VERDICT)
    assert pauses[0] == pytest.approx(4)
