from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from quant_models.macro_filter import MacroCalendarFilter


@pytest.mark.asyncio
async def test_macro_filter_logic():
    filter_ = MacroCalendarFilter(blackout_minutes_before=15, blackout_minutes_after=15)

    # By default, if no events fetched, should allow
    assert filter_.is_trading_allowed() is True

    # Let's manually inject an event happening now
    now = datetime.now(timezone.utc)

    with patch.object(filter_, "_fetch_sync") as mock_fetch:
        # Mock the live fetch returning an event for 'now'
        mock_fetch.return_value = [now]
        await filter_.fetch_events()

        # Since event is exactly now, we are in the blackout window
        assert filter_.is_trading_allowed() is False

        # Check event 20 mins ago (outside the 15 min after window)
        filter_._events = [now - timedelta(minutes=20)]
        assert filter_.is_trading_allowed() is True

        # Check event in 10 mins (inside the 15 min before window)
        filter_._events = [now + timedelta(minutes=10)]
        assert filter_.is_trading_allowed() is False
