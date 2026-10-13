from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Final

import aiohttp

from config.settings import SETTINGS

_LOG: Final[logging.Logger] = logging.getLogger(__name__)


class DiscordAlerter:
    """Async fire-and-forget Discord webhook client for critical trading system events.

    Reads the webhook URL from ``SETTINGS.alerting.discord_webhook_url`` by default.
    When no URL is configured every call is a no-op so the alerter can be left
    instantiated in all environments without side effects.
    """

    def __init__(self, webhook_url: str = "") -> None:
        """Initialise the alerter.

        Args:
            webhook_url: Override the URL sourced from settings.  Useful in tests.
        """
        self._webhook_url: str = webhook_url or SETTINGS.alerting.discord_webhook_url

    async def send_alert(
        self,
        title: str,
        description: str,
        color: int = 0xFF0000,
        fields: list[dict[str, object]] | None = None,
    ) -> None:
        """POST a Discord embed to the configured webhook URL.

        The method is intentionally fault-tolerant: any network failure or HTTP
        error is caught, logged at ERROR level, and silently swallowed so that
        a Discord outage can never interrupt the trading engine's critical path.

        Args:
            title: Embed title shown in bold at the top of the Discord card.
            description: Body text of the embed.
            color: Left-border colour as a 24-bit integer (default: red 0xFF0000).
            fields: Optional Discord embed field objects (name/value/inline).
        """
        if not self._webhook_url:
            _LOG.warning(
                "[DISCORD ALERTER] No webhook URL configured — alert '%s' suppressed.",
                title,
            )
            return

        embed: dict[str, object] = {
            "title": title,
            "description": description,
            "color": color,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if fields:
            embed["fields"] = fields

        payload: dict[str, object] = {"embeds": [embed]}

        try:
            async with (
                aiohttp.ClientSession() as session,
                session.post(
                    self._webhook_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=10.0),
                ) as response,
            ):
                if response.status not in {200, 204}:
                    _LOG.error(
                        "[DISCORD ALERTER] Webhook rejected alert '%s' — HTTP %d.",
                        title,
                        response.status,
                    )
                else:
                    _LOG.info(
                        "[DISCORD ALERTER] Alert '%s' delivered (HTTP %d).",
                        title,
                        response.status,
                    )
        except (TimeoutError, aiohttp.ClientError) as exc:
            _LOG.error(
                "[DISCORD ALERTER] Failed to deliver alert '%s' — %s",
                title,
                exc,
            )
