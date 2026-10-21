"""Tests for CCXTBroker.execute_signal using fully mocked ccxt exchange objects.

All coroutines that would touch a real exchange (fetch_balance, create_market_buy_order,
price_to_precision, amount_to_precision) are replaced with AsyncMock / MagicMock so that
no network request is ever issued.
"""

from __future__ import annotations

import pathlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from execution.ccxt_broker import CCXTBroker

_BULLISH: str = "BULLISH_ABSORPTION"
_BEARISH: str = "BEARISH_ABSORPTION"
_ENTRY_PRICE: float = 50_000.0
_CURRENT_TIME: float = 9_999_999.0


@pytest.fixture
def clean_broker(tmp_path: pathlib.Path, mock_exchange: MagicMock) -> CCXTBroker:
    """CCXTBroker with a blank state — no position, no fault, no cooldown."""
    state_file = tmp_path / "live_state.json"

    with patch("execution.ccxt_broker.ccxt") as mock_ccxt:
        mock_ccxt.bybit.return_value = mock_exchange
        broker = CCXTBroker(
            exchange_id="bybit",
            api_key="test_key",
            api_secret="test_secret",
            testnet=False,
            symbol="BTC/USDT",
            risk_per_trade_pct=0.01,
            base_sl_usdt_dist=150.0,
            tp_usdt_dist=300.0,
            fee_rate=0.0004,
            trailing_activation_usdt=100.0,
            cooldown_s=0.0,
            max_daily_loss_usdt=500.0,
            max_order_size_btc=10.0,
            state_file=state_file,
        )

    return broker


class TestPrecisionCallOrdering:
    """amount_to_precision and price_to_precision are always called before the exchange order."""

    async def test_precision_methods_invoked_before_buy_order(
        self, clean_broker: CCXTBroker, mock_exchange: MagicMock
    ) -> None:
        """For a BULLISH signal the precision helpers must be called strictly before create_market_buy_order."""
        call_log: list[str] = []

        def amount_side(sym: str, amt: float) -> str:
            call_log.append("amount_to_precision")
            return str(round(float(amt), 6))

        def price_side(sym: str, price: float) -> str:
            call_log.append("price_to_precision")
            return str(round(float(price), 2))

        async def buy_side(*args: object, **kwargs: object) -> dict[str, str]:
            call_log.append("create_market_buy_order")
            return {"id": "mock_buy_001"}

        mock_exchange.amount_to_precision = MagicMock(side_effect=amount_side)
        mock_exchange.price_to_precision = MagicMock(side_effect=price_side)
        mock_exchange.create_market_buy_order = AsyncMock(side_effect=buy_side)

        await clean_broker.execute_signal(
            signal_type=_BULLISH,
            current_price=_ENTRY_PRICE,
            current_time=_CURRENT_TIME,
        )

        assert "amount_to_precision" in call_log
        assert "price_to_precision" in call_log
        assert "create_market_buy_order" in call_log
        assert call_log.index("amount_to_precision") < call_log.index("create_market_buy_order")
        assert call_log.index("price_to_precision") < call_log.index("create_market_buy_order")

    async def test_precision_methods_invoked_before_sell_order(
        self, clean_broker: CCXTBroker, mock_exchange: MagicMock
    ) -> None:
        """For a BEARISH signal the precision helpers must be called before create_market_sell_order."""
        call_log: list[str] = []

        def amount_side(sym: str, amt: float) -> str:
            call_log.append("amount_to_precision")
            return str(round(float(amt), 6))

        def price_side(sym: str, price: float) -> str:
            call_log.append("price_to_precision")
            return str(round(float(price), 2))

        async def sell_side(*args: object, **kwargs: object) -> dict[str, str]:
            call_log.append("create_market_sell_order")
            return {"id": "mock_sell_001"}

        mock_exchange.amount_to_precision = MagicMock(side_effect=amount_side)
        mock_exchange.price_to_precision = MagicMock(side_effect=price_side)
        mock_exchange.create_market_sell_order = AsyncMock(side_effect=sell_side)

        await clean_broker.execute_signal(
            signal_type=_BEARISH,
            current_price=_ENTRY_PRICE,
            current_time=_CURRENT_TIME,
        )

        assert call_log.index("amount_to_precision") < call_log.index("create_market_sell_order")
        assert call_log.index("price_to_precision") < call_log.index("create_market_sell_order")

    async def test_amount_to_precision_receives_computed_size(
        self, clean_broker: CCXTBroker, mock_exchange: MagicMock
    ) -> None:
        """amount_to_precision must be called with a positive, non-zero size argument."""
        captured_sizes: list[float] = []

        def amount_side(sym: str, amt: float) -> str:
            captured_sizes.append(float(amt))
            return str(round(float(amt), 6))

        mock_exchange.amount_to_precision = MagicMock(side_effect=amount_side)

        await clean_broker.execute_signal(
            signal_type=_BULLISH,
            current_price=_ENTRY_PRICE,
            current_time=_CURRENT_TIME,
        )

        assert len(captured_sizes) == 1
        assert captured_sizes[0] > 0.0


class TestKillSwitch:
    """execute_signal is frozen when session PnL exceeds the daily loss limit."""

    async def test_buy_order_never_placed_when_daily_loss_exceeded(
        self, clean_broker: CCXTBroker, mock_exchange: MagicMock
    ) -> None:
        """create_market_buy_order must not be called when session PnL is at or below the kill-switch threshold."""
        clean_broker._session_pnl = -(clean_broker._max_daily_loss_usdt + 0.01)

        await clean_broker.execute_signal(
            signal_type=_BULLISH,
            current_price=_ENTRY_PRICE,
            current_time=_CURRENT_TIME,
        )

        mock_exchange.create_market_buy_order.assert_not_called()

    async def test_sell_order_never_placed_when_daily_loss_exceeded(
        self, clean_broker: CCXTBroker, mock_exchange: MagicMock
    ) -> None:
        """create_market_sell_order must not be called when session PnL is at or below the kill-switch threshold."""
        clean_broker._session_pnl = -(clean_broker._max_daily_loss_usdt + 0.01)

        await clean_broker.execute_signal(
            signal_type=_BEARISH,
            current_price=_ENTRY_PRICE,
            current_time=_CURRENT_TIME,
        )

        mock_exchange.create_market_sell_order.assert_not_called()

    async def test_kill_switch_triggers_at_exact_limit(
        self, clean_broker: CCXTBroker, mock_exchange: MagicMock
    ) -> None:
        """The kill-switch must also fire when session PnL equals exactly -max_daily_loss_usdt."""
        clean_broker._session_pnl = -clean_broker._max_daily_loss_usdt

        await clean_broker.execute_signal(
            signal_type=_BULLISH,
            current_price=_ENTRY_PRICE,
            current_time=_CURRENT_TIME,
        )

        mock_exchange.create_market_buy_order.assert_not_called()

    async def test_order_is_placed_when_pnl_is_within_limit(
        self, clean_broker: CCXTBroker, mock_exchange: MagicMock
    ) -> None:
        """An order must reach the exchange when PnL is still within the daily loss limit."""
        clean_broker._session_pnl = -(clean_broker._max_daily_loss_usdt - 1.0)

        await clean_broker.execute_signal(
            signal_type=_BULLISH,
            current_price=_ENTRY_PRICE,
            current_time=_CURRENT_TIME,
        )

        mock_exchange.create_market_buy_order.assert_called_once()

    async def test_execute_signal_uses_position_sizer(self, clean_broker: CCXTBroker, mock_exchange: MagicMock) -> None:
        """When position_sizer is configured, Kelly sizing overrides fixed risk fraction."""
        from quant_models.position_sizer import PositionSizer

        sizer = PositionSizer(base_win_rate=0.55, win_loss_ratio=2.0, half_kelly=True)
        clean_broker._position_sizer = sizer

        # Balance = 10000, risk capital = 500, base SL dist = 150, gex_value=0.02 (mult=1.0) -> 500 / 150 = 3.333333 BTC
        await clean_broker.execute_signal(
            signal_type=_BULLISH,
            current_price=_ENTRY_PRICE,
            current_time=_CURRENT_TIME,
            gex_value=0.02,
        )

        mock_exchange.create_market_buy_order.assert_called_once()
        call_args = mock_exchange.create_market_buy_order.call_args[0]
        # call_args[1] is amount
        assert abs(call_args[1] - (500.0 / 150.0)) < 1e-4

    async def test_execute_signal_applies_sentiment_modifier(
        self, clean_broker: CCXTBroker, mock_exchange: MagicMock
    ) -> None:
        """Adverse sentiment scales down position size via Kelly sizer."""
        from quant_models.position_sizer import PositionSizer

        sizer = PositionSizer(base_win_rate=0.55, win_loss_ratio=2.0, half_kelly=True)
        clean_broker._position_sizer = sizer

        # Balance = 10000, risk = 500, SL = 150.
        # sentiment_score = -2.0 for BULLISH -> modifier 0.8 -> risk = 400.
        # Expected size: 400 / 150 = 2.666667 BTC
        await clean_broker.execute_signal(
            signal_type=_BULLISH,
            current_price=_ENTRY_PRICE,
            current_time=_CURRENT_TIME,
            gex_value=0.02,
            sentiment_score=-2.0,
        )

        mock_exchange.create_market_buy_order.assert_called_once()
        call_args = mock_exchange.create_market_buy_order.call_args[0]
        assert abs(call_args[1] - (400.0 / 150.0)) < 1e-4

    async def test_order_size_clamped_to_max_order_size_btc(
        self, clean_broker: CCXTBroker, mock_exchange: MagicMock
    ) -> None:
        """Calculated position sizes exceeding max_order_size_btc must be clamped."""
        from quant_models.position_sizer import PositionSizer

        clean_broker._max_order_size_btc = 0.05
        sizer = PositionSizer(base_win_rate=0.55, win_loss_ratio=2.0, half_kelly=True)
        clean_broker._position_sizer = sizer

        await clean_broker.execute_signal(
            signal_type=_BULLISH,
            current_price=_ENTRY_PRICE,
            current_time=_CURRENT_TIME,
            gex_value=0.02,
        )

        mock_exchange.create_market_buy_order.assert_called_once()
        call_args = mock_exchange.create_market_buy_order.call_args[0]
        assert call_args[1] == 0.05

    async def test_consecutive_errors_trigger_circuit_breaker(
        self, clean_broker: CCXTBroker, mock_exchange: MagicMock
    ) -> None:
        """After max_consecutive_errors failed attempts, trading must freeze into system_fault."""
        import ccxt

        mock_exchange.create_market_buy_order.side_effect = ccxt.NetworkError("Connection lost")
        clean_broker._max_consecutive_errors = 3

        for i in range(1, 4):
            clean_broker._last_trade_time = 0.0  # bypass cooldown for test invocation
            await clean_broker.execute_signal(
                signal_type=_BULLISH,
                current_price=_ENTRY_PRICE,
                current_time=_CURRENT_TIME + i,
            )

        assert clean_broker._consecutive_errors == 3
        assert clean_broker._system_fault is True

    async def test_unrealized_drawdown_kill_switch_triggers_emergency_exit(
        self, clean_broker: CCXTBroker, mock_exchange: MagicMock
    ) -> None:
        """When unrealized loss on an open position breaches max_daily_loss_usdt, close position and freeze."""
        clean_broker._position = "LONG"
        clean_broker._entry_price = 50_000.0
        clean_broker._position_btc = 0.1  # $5,000 notional
        clean_broker._max_daily_loss_usdt = 500.0
        clean_broker._session_pnl = 0.0

        # Price drops to $44,000 -> unrealized loss = 0.1 * (44,000 - 50,000) = -600 USD (breaches -500 limit)
        current_crash_price = 44_000.0
        await clean_broker.update_tick(current_price=current_crash_price, current_time=_CURRENT_TIME)

        # Market sell order must have been placed to close position
        mock_exchange.create_market_sell_order.assert_called()
        assert clean_broker._system_fault is True

    async def test_cancel_bracket_orders_hyperliquid_fallback(
        self, clean_broker: CCXTBroker, mock_exchange: MagicMock
    ) -> None:
        """When cancel_all_orders is not supported (Hyperliquid), fallback to fetchOpenOrders + cancel_order."""
        mock_exchange.has = {"cancelAllOrders": False, "fetchOpenOrders": True, "cancelOrders": False}
        mock_exchange.fetch_open_orders = AsyncMock(return_value=[{"id": "order_123"}, {"id": "order_456"}])
        mock_exchange.cancel_order = AsyncMock()

        ok = await clean_broker._cancel_all_bracket_orders()

        assert ok is True
        assert mock_exchange.cancel_order.call_count == 2
