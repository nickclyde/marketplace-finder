import httpx

from marketplace_finder.notify import Notifications, Notifier


def test_nothing_is_sent_without_a_topic(config):
    notifier = Notifier(config)
    assert not notifier.configured
    assert notifier.send({"title": "t", "message": "m"}) is False


def test_alert_goes_to_the_ntfy_topic(config, monkeypatch):
    config.ntfy_topic = "private-topic"
    sent = []

    def post(url, json, timeout):
        sent.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    assert Notifier(config).send_alert("CRT finder: craigslist is failing", "3 runs in a row.")
    url, payload = sent[0]
    assert url == "https://ntfy.sh"
    assert payload["topic"] == "private-topic" and payload["tags"] == ["warning"]


class FakeDiscord:
    def __init__(self):
        self.sent = []

    def send_match(self, listing, verdict, **kwargs):
        self.sent.append(("match", listing.source_id, kwargs))
        return True

    def send_text(self, text):
        self.sent.append(("text", text))
        return True


class FakeAlerts:
    def __init__(self):
        self.sent = []

    def send_alert(self, title, message):
        self.sent.append(title)
        return True


def test_listings_go_to_discord_and_alerts_to_ntfy(make_listing):
    discord, alerts = FakeDiscord(), FakeAlerts()
    notifications = Notifications(discord, alerts)
    assert notifications.send_match(make_listing(), {}, rank=(1, 10), reason="cheap")
    assert notifications.send_text("welcome")
    assert notifications.send_alert("CRT finder: facebook needs you", "log in again")
    assert discord.sent == [("match", "1", {"rank": (1, 10), "reason": "cheap"}), ("text", "welcome")]
    # The channel never sees an operational alert.
    assert alerts.sent == ["CRT finder: facebook needs you"]
