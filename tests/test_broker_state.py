"""Tests for CCXTBroker state recovery from a persisted live_state.json.

The ccxt exchange is patched at the module level so no network connection is
ever attempted.  The fixture writes a synthetic state file containing an active
LONG position with _system_fault=True, then verifies that the broker re-hydrates
all fields correctly and that execute_signal is unconditionally blocked.
"""

from __future__ import annotations

import json
import pathlib
from unittest.mock import MagicMock, patch

import pytest

from execution.ccxt_broker import CCXTBroker

_FAULT_STATE: dict[str, object] = {
    "position": "LONG",
    "entry_price": 50_000.0,
    "position_btc": 0.02,
    "current_local_sl_price": 49_850.0,
    "last_trade_time": 1_000.0,
    "session_pnl": -250.0,
    "system_fault": True,
}


@pytest.fixture
def fault_broker(tmp_path: pathlib.Path, mock_exchange: MagicMock) -> CCXTBroker:
    """CCXTBroker initialised from a state file carrying an active LONG + system_fault=True."""
    state_file = tmp_path / "live_state.json"
    state_file.write_text(json.dumps(_FAULT_STATE), encoding="utf-8")

    with patch("execution.ccxt_broker.ccxt") as mock_ccxt:
        mock_ccxt.bybit.return_value = mock_exchange
        broker = CCXTBroker(
            exchange_id="bybit",
            api_key="test_key",
            api_secret="test_secret",
            testnet=False,
            state_file=state_file,
        )

    return broker


class TestStateRecovery:
    """CCXTBroker correctly restores position state from disk on cold start."""

    def test_has_open_position_is_true_after_recovery(self, fault_broker: CCXTBroker) -> None:
        """has_open_position must be True when the saved state contains a LONG position."""
        assert fault_broker.has_open_position is True

    def test_session_pnl_matches_persisted_value(self, fault_broker: CCXTBroker) -> None:
        """_session_pnl must be restored exactly from the state file."""
        assert fault_broker._session_pnl == pytest.approx(_FAULT_STATE["session_pnl"])

    def test_system_fault_flag_is_set(self, fault_broker: CCXTBroker) -> None:
        """_system_fault must be True when the saved state carries system_fault=True."""
        assert fault_broker._system_fault is True

    def test_entry_price_is_restored(self, fault_broker: CCXTBroker) -> None:
        """_entry_price must equal the value serialised in the state file."""
        assert fault_broker._entry_price == pytest.approx(_FAULT_STATE["entry_price"])

    def test_position_direction_is_restored(self, fault_broker: CCXTBroker) -> None:
        """_position must be 'LONG' as serialised."""
        assert fault_broker._position == "LONG"


class TestSignalBlockedOnSystemFault:
    """execute_signal is completely frozen when _system_fault is True."""

    async def test_execute_signal_does_not_place_buy_order(
        self, fault_broker: CCXTBroker, mock_exchange: MagicMock
    ) -> None:
        """No exchange order must be submitted when the broker is in system-fault state."""
        await fault_broker.execute_signal(
            signal_type="BULLISH_ABSORPTION",
            current_price=50_000.0,
            current_time=9_999_999.0,
        )

        mock_exchange.create_market_buy_order.assert_not_called()

    async def test_execute_signal_does_not_place_sell_order(
        self, fault_broker: CCXTBroker, mock_exchange: MagicMock
    ) -> None:
        """Bearish signals are equally blocked while the system fault is active."""
        await fault_broker.execute_signal(
            signal_type="BEARISH_ABSORPTION",
            current_price=50_000.0,
            current_time=9_999_999.0,
        )

        mock_exchange.create_market_sell_order.assert_not_called()

    async def test_position_state_unchanged_after_blocked_signal(self, fault_broker: CCXTBroker) -> None:
        """Blocking a signal must not mutate any position fields on the broker."""
        position_before = fault_broker._position
        entry_before = fault_broker._entry_price

        await fault_broker.execute_signal(
            signal_type="BULLISH_ABSORPTION",
            current_price=51_000.0,
            current_time=9_999_999.0,
        )

        assert fault_broker._position == position_before
        assert fault_broker._entry_price == entry_before
