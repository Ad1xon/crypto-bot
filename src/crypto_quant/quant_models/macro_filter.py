import asyncio
import json
import logging
import urllib.request
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# ForexFactory / FairEconomy JSON endpoint for this week's events
_CALENDAR_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


class MacroCalendarFilter:
    """Pauses trading around high-impact macroeconomic events using live calendar data."""

    def __init__(self, blackout_minutes_before: int = 15, blackout_minutes_after: int = 15):
        self.blackout_before = timedelta(minutes=blackout_minutes_before)
        self.blackout_after = timedelta(minutes=blackout_minutes_after)
        self._events: list[datetime] = []
        self._last_fetch: datetime | None = None

    @property
    def last_fetch(self) -> datetime | None:
        """UTC timestamp of the most recent calendar fetch."""
        return self._last_fetch

    def _fetch_sync(self) -> list[datetime]:
        events = []
        try:
            req = urllib.request.Request(_CALENDAR_URL, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as response:
                data = json.loads(response.read().decode("utf-8"))
                for item in data:
                    # We care primarily about USD high impact (red folder) events
                    if item.get("impact") == "High" and item.get("country") == "USD":
                        # Format is usually '2023-10-04T08:30:00-04:00'
                        event_time = datetime.fromisoformat(item["date"]).astimezone(UTC)
                        events.append(event_time)
        except Exception as e:
            logger.error("Failed to fetch macroeconomic calendar from %s: %s", _CALENDAR_URL, e)
        return events

    async def fetch_events(self) -> None:
        """Fetch this week's high impact events asynchronously."""
        logger.info("Fetching live macro calendar events...")
        events = await asyncio.to_thread(self._fetch_sync)
        if events:
            self._events = events
            self._last_fetch = datetime.now(UTC)
            logger.info("Loaded %d high-impact USD events for this week.", len(self._events))
        else:
            logger.warning("No events loaded or fetch failed. Trading will not be blocked.")

    def is_trading_allowed(self, at_time: datetime | float | None = None) -> bool:
        """Check if target time (or current UTC time) is outside any blackout windows."""
        if not self._events:
            return True

        if at_time is None:
            check_time = datetime.now(UTC)
        elif isinstance(at_time, (int, float)):
            ts_s = at_time / 1000.0 if at_time > 1e11 else float(at_time)
            check_time = datetime.fromtimestamp(ts_s, tz=UTC)
        else:
            check_time = at_time.astimezone(UTC) if at_time.tzinfo else at_time.replace(tzinfo=UTC)

        for event_time in self._events:
            blackout_start = event_time - self.blackout_before
            blackout_end = event_time + self.blackout_after
            if blackout_start <= check_time <= blackout_end:
                logger.info("Trading paused: Within blackout window of macro event at %s", event_time)
                return False
        return True
