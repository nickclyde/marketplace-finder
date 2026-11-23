import stat

import httpx
import pytest

from marketplace_finder import discord
from marketplace_finder.discord import DiscordNotifier, discord_post, embed_args
from marketplace_finder.profiles import crt
from marketplace_finder.profiles.base import GREY
from marketplace_finder.profiles.crt import badges

VERDICT = {
    "is_crt_display": True, "kind": "tv", "brand": "Sony", "model_number": "KV-27FS120",
    "is_trinitron": True, "screen_size_inches": 27, "working": "unknown",
    "best_input": "component", "input_evidence": "model_knowledge", "rgb_moddable": "no",
    "summary": "27 inch flat-tube Trinitron.",
    "concerns": ["remote included?", "any geometry issues?"],
}
REFERENCE = {
    "name": "Sony KV-27FS120", "matched": "exact",
    "url": "https://crtdatabase.com/crts/sony/sony-kv-27fs120",
    "summary": "Sony's base 27 inch set for 2002.",
    "specs": {"Input Signals": "Composite, S-Video, Component YPbPr", "Mods": "RGB | S-Video"},
}


def test_post(config, make_listing):
    listing = make_listing()
    listing.distance_mi = 16.4
    post = discord_post(listing, VERDICT, config)
    assert post == {
        "author": "⭐ Trinitron",
        "title": 'Sony KV-27FS120 · 27" · $150 · Roseville, CA',
        "url": listing.url,
        "description": "27 inch flat-tube Trinitron.\n"
        "**Confirm:** remote included?; any geometry issues?",
        "fields": [
            ("Best input", "Component (per the model number)", True),
            ("Condition", "Not stated", True),
        ],
        # The area's name, not the postal code: posts are read by a whole channel.
        "footer": "craigslist · 16 mi from Sacramento",
        "color": crt.GOLD,
    }


@pytest.mark.parametrize("changes,expected", [
    ({}, ["⭐ Trinitron"]),
    ({"is_trinitron": False}, []),
    ({"rgb_moddable": "yes"}, ["⭐ Trinitron", "🌈 RGB moddable"]),
    ({"is_trinitron": False, "rgb_moddable": "yes"}, ["🌈 RGB moddable"]),
    ({"rgb_moddable": "unknown"}, ["⭐ Trinitron"]),
    # A video monitor with RGB as built.
    ({"kind": "video_monitor", "best_input": "rgb"}, ["⭐ Trinitron", "🌈 RGB"]),
    # A PC monitor is labelled as one, and its input is never called RGB.
    ({"kind": "pc_monitor", "best_input": "vga"}, ["⭐ Trinitron", "🖥️ PC monitor"]),
    ({"is_trinitron": False, "kind": "pc_monitor", "best_input": "rgb"}, ["🖥️ PC monitor"]),
    # Combos say what is built in.
    ({"has_vcr": True}, ["⭐ Trinitron", "📼 VCR"]),
    ({"is_trinitron": False, "has_dvd": True}, ["📀 DVD"]),
    ({"is_trinitron": False, "has_vcr": True, "has_dvd": True}, ["📼 VCR", "📀 DVD"]),
    ({"has_vcr": False, "has_dvd": False}, ["⭐ Trinitron"]),
])
def test_badges(changes, expected):
    assert badges({**VERDICT, **changes}) == expected


def test_colour_follows_the_flair(config, make_listing):
    plain = {**VERDICT, "is_trinitron": False}
    assert discord_post(make_listing(), plain, config)["color"] == GREY
    moddable = {**plain, "rgb_moddable": "yes"}
    assert discord_post(make_listing(), moddable, config)["color"] == crt.TEAL


def test_post_for_another_brand_without_a_model(config, make_listing):
    listing = make_listing(title="Old JVC tube TV", location_text="", lat=None, lon=None)
    verdict = {**VERDICT, "brand": "JVC", "model_number": None, "is_trinitron": False,
               "screen_size_inches": None, "working": "yes", "best_input": "unknown", "concerns": []}
    post = discord_post(listing, verdict, config)
    assert post["title"] == "Old JVC tube TV · $150"
    # Sellers put line breaks in titles; a post title is one line.
    listing.title = "Panasonic CT-25G7F-1\n25-inch CRT"
    assert discord_post(listing, verdict, config)["title"] == "Panasonic CT-25G7F-1 25-inch CRT · $150"
    assert post["author"] == "" and post["footer"] == "craigslist"
    assert post["fields"] == [("Best input", "Not known", True),
                              ("Condition", "Working, per the seller", True)]


def test_brand_is_added_to_a_bare_model_number(config, make_listing):
    verdict = {**VERDICT, "brand": "JVC", "model_number": "AV-27D302", "is_trinitron": False}
    assert discord_post(make_listing(), verdict, config)["title"].startswith("JVC AV-27D302 · ")
    verdict = {**VERDICT, "model_number": "Sony KV-27FS120"}
    assert discord_post(make_listing(), verdict, config)["title"].startswith("Sony KV-27FS120 · ")


def test_reference_entry_and_pick_details(config, make_listing):
    verdict = {**VERDICT, "input_evidence": "crt_database", "reference": REFERENCE}
    post = discord_post(make_listing(), verdict, config, rank=(3, 10), reason="Cheap for a 27.")
    assert post["author"] == "#3 of 10 · ⭐ Trinitron"
    assert post["description"].splitlines()[0] == "Cheap for a 27."
    name, value, inline = post["fields"][-1]
    assert (name, inline) == ("CRT Database", False)
    assert value.splitlines() == [
        "[Sony KV-27FS120](https://crtdatabase.com/crts/sony/sony-kv-27fs120)",
        "Inputs: Composite, S-Video, Component YPbPr",
        "Mods: RGB / S-Video",  # a "|" would be read by discli as the end of the field
    ]
    closest = {**verdict, "reference": {**REFERENCE, "matched": "closest"}}
    assert "closest entry" in discord_post(make_listing(), closest, config)["fields"][-1][1]


def weight_field(post):
    return next((value for name, value, _ in post["fields"] if name == "Weight"), None)


def test_weight_comes_from_the_reference_entry_or_the_listing(config, make_listing):
    listing = make_listing()
    heavy = {**REFERENCE, "specs": {**REFERENCE["specs"], "Weight": "103 lbs (46.7 kg)"}}
    # The entry's figure for the model wins over whatever the listing claims.
    post = discord_post(listing, {**VERDICT, "weight_lbs": 90, "reference": heavy}, config)
    assert weight_field(post) == "103 lbs (46.7 kg), per CRT Database"
    assert [name for name, _, _ in post["fields"]] == ["Best input", "Condition", "Weight", "CRT Database"]
    assert post["fields"][2][2] is True  # sits beside the other two

    closest = {**VERDICT, "reference": {**heavy, "matched": "closest"}}
    assert weight_field(discord_post(listing, closest, config)) == (
        "103 lbs (46.7 kg), per CRT Database, closest entry"
    )
    # An entry with no weight leaves it to the listing.
    stated = {**VERDICT, "weight_lbs": 62.5, "reference": REFERENCE}
    assert weight_field(discord_post(listing, stated, config)) == "62.5 lbs, per the listing"
    assert weight_field(discord_post(listing, {**VERDICT, "weight_lbs": 40}, config)) == "40 lbs, per the listing"


@pytest.mark.parametrize("weight", [None, 0, -5, "heavy", True])
def test_no_weight_field_when_nobody_states_one(config, make_listing, weight):
    post = discord_post(make_listing(), {**VERDICT, "weight_lbs": weight}, config)
    assert weight_field(post) is None
    assert weight_field(discord_post(make_listing(), VERDICT, config)) is None  # older verdicts


def test_listing_text_is_made_inert(config, make_listing):
    listing = make_listing(title="**FREE** [click](https://evil.example) @everyone | `tv`\\n")
    verdict = {
        **VERDICT, "model_number": None,
        "summary": "See https://evil.example/pay for [photos](https://evil.example).",
        "concerns": ["> quoted | piped"],
    }
    post = discord_post(listing, verdict, config)
    assert "https://" not in post["title"] + post["description"]
    assert "\\*\\*FREE\\*\\*" in post["title"] and "\\[click\\]" in post["title"]
    assert "\\`tv\\`/n" in post["title"]  # no backslash-n left for discli to turn into a newline
    assert "\\[photos\\](evil.example)" in post["description"]
    assert "\\> quoted \\| piped" in post["description"]
    for name, value, _ in post["fields"]:
        assert "|" not in name + value


def test_things_to_confirm_are_kept_short(config, make_listing):
    verdict = {**VERDICT, "concerns": [f"point {n} " + "and more " * 40 for n in range(8)]}
    confirm = discord_post(make_listing(), verdict, config)["description"].splitlines()[-1]
    assert confirm.count(";") == 3 and "point 3" in confirm and "point 4" not in confirm
    assert len(confirm) < 4 * 150


def test_long_text_is_cut_to_discords_limits(config, make_listing):
    verdict = {**VERDICT, "model_number": None, "summary": "word " * 2000}
    post = discord_post(make_listing(title="Sony " * 200), verdict, config)
    assert len(post["title"]) <= 256 and len(post["description"]) <= 4096
    assert post["description"].endswith("…")


def test_arguments_cannot_become_options(config, make_listing):
    listing = make_listing(title="--content=@everyone free tv")
    post = discord_post(listing, {**VERDICT, "model_number": None}, config)
    args = embed_args("listings", post)
    assert args[:3] == ["message", "embed", "listings"]
    # Every value rides on its own option, and nothing is ever sent as message content.
    assert all(arg.startswith("--") and "=" in arg for arg in args[3:])
    assert not any(arg.startswith("--content") for arg in args)
    assert args[3].startswith("--title=--content=@everyone free tv")
    assert "--field=Best input|Component (per the model number)|inline" in args
    assert not any(arg.startswith("--image") for arg in args)


def fake_discli(tmp_path, body="exit 0"):
    script = tmp_path / "discli"
    script.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {tmp_path / 'args.txt'}\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


def notifier(config, tmp_path, pauses=None, **kwargs):
    pauses = [] if pauses is None else pauses
    return DiscordNotifier(config, command=fake_discli(tmp_path, **kwargs), pause=pauses.append)


def test_send_runs_discli_with_the_channel_from_config(config, make_listing, tmp_path):
    pauses = []
    assert notifier(config, tmp_path, pauses).send_match(make_listing(), VERDICT)
    args = (tmp_path / "args.txt").read_text().splitlines()
    assert args[:3] == ["message", "embed", "listings"]
    assert '--title=Sony KV-27FS120 · 27" · $150 · Roseville, CA' in args
    assert pauses == [discord.PAUSE_BETWEEN_POSTS]


def test_channel_can_be_overridden_for_testing(config, make_listing, tmp_path):
    sender = DiscordNotifier(
        config, channel="testing", command=fake_discli(tmp_path), pause=lambda _: None
    )
    assert sender.send_match(make_listing(), VERDICT)
    assert (tmp_path / "args.txt").read_text().splitlines()[2] == "testing"


def test_photo_is_uploaded_from_a_local_copy(config, make_listing, tmp_path, monkeypatch):
    def handler(request):
        return httpx.Response(200, content=b"jpeg bytes", headers={"content-type": "image/jpeg"})

    real_client = httpx.Client
    monkeypatch.setattr(
        "marketplace_finder.photos.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler)),
    )
    listing = make_listing(image_urls=["https://img.example/1.jpg", "https://img.example/2.jpg"])
    assert notifier(config, tmp_path).send_match(listing, VERDICT)
    image = [a for a in (tmp_path / "args.txt").read_text().splitlines() if a.startswith("--image=")]
    # A path to the downloaded copy, never the listing's own URL.
    assert len(image) == 1 and image[0].endswith("/photo1.jpg") and "img.example" not in image[0]


def test_failed_photo_download_still_posts(config, make_listing, tmp_path, monkeypatch):
    real_client = httpx.Client
    monkeypatch.setattr(
        "marketplace_finder.photos.httpx.Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(404))),
    )
    assert notifier(config, tmp_path).send_match(make_listing(image_urls=["https://x/1.jpg"]), VERDICT)
    assert "--image" not in (tmp_path / "args.txt").read_text()


def test_discli_failure_is_reported(config, make_listing, tmp_path):
    pauses = []
    failing = notifier(config, tmp_path, pauses, body="echo '403 Forbidden' >&2; exit 4")
    assert failing.send_match(make_listing(), VERDICT) is False
    assert pauses == []


def rate_limited_discli(tmp_path, times, seconds="35.173"):
    """A discli that is rate limited `times` times before it succeeds."""
    count = tmp_path / "count"
    count.write_text("0")
    return (
        f"n=$(cat {count}); echo $((n + 1)) > {count}\n"
        f"if [ $n -lt {times} ]; then echo 'Rate limited. Retry after {seconds}s.' >&2; exit 1; fi"
    )


def test_rate_limit_is_waited_out_and_the_post_sent_again(config, make_listing, tmp_path):
    pauses = []
    sender = notifier(config, tmp_path, pauses, body=rate_limited_discli(tmp_path, times=1))
    assert sender.send_match(make_listing(), VERDICT) is True
    # The wait Discord asked for and a second on top, then the usual gap after a post.
    assert pauses == [pytest.approx(36.173), discord.PAUSE_BETWEEN_POSTS]
    assert (tmp_path / "count").read_text().strip() == "2"


def test_rate_limit_that_does_not_clear_is_a_failure(config, make_listing, tmp_path):
    pauses = []
    sender = notifier(config, tmp_path, pauses, body=rate_limited_discli(tmp_path, times=9))
    assert sender.send_match(make_listing(), VERDICT) is False
    assert len(pauses) == discord.RATE_LIMIT_RETRIES
    assert (tmp_path / "count").read_text().strip() == "3"


def test_a_very_long_rate_limit_is_left_for_the_next_run(config, make_listing, tmp_path):
    pauses = []
    body = rate_limited_discli(tmp_path, times=9, seconds="900")
    assert notifier(config, tmp_path, pauses, body=body).send_match(make_listing(), VERDICT) is False
    assert pauses == []


def test_missing_discli_is_reported(config, make_listing, tmp_path):
    sender = DiscordNotifier(config, command=str(tmp_path / "no-such-discli"))
    assert sender.send_match(make_listing(), VERDICT) is False


def test_nothing_is_sent_when_discord_is_off(config, make_listing, tmp_path):
    config.discord_enabled = False
    assert notifier(config, tmp_path).send_match(make_listing(), VERDICT) is False
    assert not (tmp_path / "args.txt").exists()


def test_welcome_text_is_sent_as_a_plain_message(config, tmp_path):
    assert notifier(config, tmp_path).send_text("**CRT hunting is live.**")
    args = (tmp_path / "args.txt").read_text().splitlines()
    assert args == ["message", "send", "listings", "--", "**CRT hunting is live.**"]


@pytest.mark.parametrize("text", [
    "hthttp://tp://evil.example", "HTTPS://hTtP://evil.example", "hhttps://ttps://https://evil.example",
])
def test_a_link_cannot_be_rebuilt_from_what_cleaning_leaves(text):
    assert "://" not in discord.clean(text) and discord.clean(text).endswith("evil.example")
