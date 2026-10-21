from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest

from quant_models.macro_filter import MacroCalendarFilter


@pytest.mark.asyncio
async def test_macro_filter_logic():
    filter_ = MacroCalendarFilter(blackout_minutes_before=15, blackout_minutes_after=15)

    # By default, if no events fetched, should allow
    assert filter_.is_trading_allowed() is True

    # Let's manually inject an event happening now
    now = datetime.now(UTC)

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


@pytest.mark.asyncio
async def test_macro_filter_last_fetch():
    filter_ = MacroCalendarFilter()
    assert filter_.last_fetch is None

    now = datetime.now(UTC)
    with patch.object(filter_, "_fetch_sync") as mock_fetch:
        mock_fetch.return_value = [now]
        await filter_.fetch_events()

        assert filter_.last_fetch is not None
        assert abs((filter_.last_fetch - now).total_seconds()) < 5.0


@pytest.mark.asyncio
async def test_live_signal_engine_macro_gate():
    from unittest.mock import AsyncMock, MagicMock

    from execution.live_engine import LiveSignalEngine
    from quant_models.super_confluence import ConfluenceVerdict

    now = datetime.now(UTC)
    macro_filter = MacroCalendarFilter()
    macro_filter._events = [now]  # Active blackout

    mock_exec = MagicMock()
    mock_exec.has_open_position = False
    mock_exec.is_on_cooldown.return_value = False
    mock_exec.update_tick = AsyncMock()
    mock_exec.execute_signal = AsyncMock()

    mock_confluence = MagicMock()
    mock_confluence.evaluate.return_value = ConfluenceVerdict(
        allowed=True, vwap_ok=True, gex_ok=True, nearest_strike=60000.0, gex_value=0.05
    )

    engine = LiveSignalEngine(
        confluence=mock_confluence,
        execution_engine=mock_exec,
        macro_filter=macro_filter,
    )

    # Fill window with dummy ticks to pass _MIN_TICKS
    now_ms = int(now.timestamp() * 1000)
    for i in range(120):
        engine._window.append(
            {
                "timestamp_ms": now_ms - 120 + i,
                "price": 60000.0,
                "cvd": 1.0,
                "ofi": 0.5,
            }
        )

    # Mock detector to emit a signal on last tick
    import pandas as pd

    engine._detector = MagicMock()
    engine._detector.smooth_ofi.side_effect = lambda df: df
    engine._detector.detect_cvd_divergence.return_value = pd.DataFrame(
        [{"timestamp_ms": now_ms, "signal": "BULLISH_ABSORPTION", "price": 60000.0, "signal_ts": 1.0}]
    )

    engine.process_tick(now_ms, 60000.0, 1.0, 0.5)

    # execute_signal should NOT have been called because macro blackout blocked it!
    mock_exec.execute_signal.assert_not_called()
