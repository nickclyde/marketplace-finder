"""Where things go: listings to the Discord channel, operational alerts to ntfy."""

from __future__ import annotations

import logging

import httpx

from .config import Config

log = logging.getLogger(__name__)


class Notifier:
    """Private alerts to the operator's phone, through ntfy."""

    def __init__(self, config: Config):
        self.server = config.ntfy_server
        self.topic = config.ntfy_topic

    @property
    def configured(self) -> bool:
        return bool(self.topic)

    def send(self, payload: dict) -> bool:
        if not self.configured:
            log.warning("NTFY_TOPIC is not set; notification not sent: %s", payload.get("title"))
            return False
        try:
            response = httpx.post(self.server, json={"topic": self.topic, **payload}, timeout=20)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.error("ntfy send failed: %s", exc)
            return False
        return True

    def send_alert(self, title: str, message: str) -> bool:
        return self.send(
            {"title": title, "message": message, "priority": 2, "tags": ["warning"]}
        )


class Notifications:
    """What the pipeline talks to. Nothing operational is ever sent to the channel."""

    def __init__(self, discord, alerts: Notifier):
        self.discord = discord
        self.alerts = alerts

    def send_match(self, listing, verdict, **kwargs) -> bool:
        return self.discord.send_match(listing, verdict, **kwargs)

    def send_text(self, text: str) -> bool:
        return self.discord.send_text(text)

    def send_alert(self, title: str, message: str) -> bool:
        return self.alerts.send_alert(title, message)
