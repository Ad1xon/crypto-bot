"""Tests for the trailing stop-loss and break-even logic inside PaperTrader._evaluate_exit.

Params chosen so that:
  position_btc       = 10_000 / 50_000 = 0.2 BTC
  sl_price_delta     = 150 / 0.2       = 750 pts
  activation_delta   = 100 / 0.2       = 500 pts  (triggers at price >= 50_500)
  true_be_price      = 50_000 + 50_000 * 0.0008 = 50_040.0
"""

from __future__ import annotations

import pathlib

from execution.live_engine import PaperTrader

_ENTRY: float = 50_000.0
_ACTIVATION_PRICE: float = 50_500.0
_ABOVE_PEAK: float = 52_000.0


def _make_trader(tmp_path: pathlib.Path, **overrides: object) -> PaperTrader:
    """Return an isolated PaperTrader; overrides replace specific constructor defaults."""
    defaults: dict[str, object] = dict(
        balance=10_000.0,
        position_size_usdt=10_000.0,
        sl_usdt=150.0,
        tp_usdt=10_000.0,
        trailing_activation_usdt=100.0,
        fee_rate=0.0004,
        cooldown_s=0.0,
        slippage_bps=0.0,
        state_file=tmp_path / "paper_state.json",
    )
    defaults.update(overrides)
    return PaperTrader(**defaults)  # type: ignore[arg-type]


def _inject_long(trader: PaperTrader, entry_price: float) -> None:
    """Directly write a synthetic LONG position into the trader's internal state."""
    trader._position = "LONG"
    trader._entry_price = entry_price
    trader._position_btc = trader._position_size_usdt / entry_price
    trader._current_sl_price = entry_price - trader._sl_usdt


class TestTrailingStopLong:
    """Trailing stop mechanics for an open LONG position."""

    def test_sl_ratchets_monotonically_above_activation_threshold(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Each successive price high above the activation level must raise the SL or keep it flat."""
        trader = _make_trader(tmp_path)
        _inject_long(trader, _ENTRY)

        prev_sl: float = trader._current_sl_price
        for price in [_ACTIVATION_PRICE, 51_000.0, 51_500.0, _ABOVE_PEAK]:
            trader._evaluate_exit(price)
            assert trader._current_sl_price >= prev_sl, (
                f"SL retreated from {prev_sl:.4f} to {trader._current_sl_price:.4f} "
                f"on price tick {price}"
            )
            prev_sl = trader._current_sl_price

    def test_sl_never_retreats_after_price_reversal(
        self, tmp_path: pathlib.Path
    ) -> None:
        """A price reversal below the most recent high must not lower the already-ratcheted SL."""
        trader = _make_trader(tmp_path)
        _inject_long(trader, _ENTRY)

        trader._evaluate_exit(_ABOVE_PEAK)
        peak_sl: float = trader._current_sl_price

        trader._evaluate_exit(51_000.0)

        assert trader._current_sl_price == peak_sl, (
            f"SL moved from {peak_sl:.4f} to {trader._current_sl_price:.4f} on reversal"
        )

    def test_sl_does_not_move_below_activation_threshold(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Price ticks below the activation threshold must leave the initial SL unchanged."""
        trader = _make_trader(tmp_path)
        _inject_long(trader, _ENTRY)
        initial_sl: float = trader._current_sl_price

        trader._evaluate_exit(_ENTRY + 100.0)

        assert trader._current_sl_price == initial_sl


class TestBreakEvenFloor:
    """Break-even price calculation and its role as the trailing SL floor."""

    def test_be_price_equals_entry_plus_round_trip_fee_coverage(
        self, tmp_path: pathlib.Path
    ) -> None:
        """The fee-aware BE price must equal entry + entry_price * 2 * fee_rate."""
        fee_rate = 0.0004
        trader = _make_trader(tmp_path, fee_rate=fee_rate)
        _inject_long(trader, _ENTRY)

        expected_be: float = _ENTRY + _ENTRY * (fee_rate * 2.0)

        trader._evaluate_exit(_ACTIVATION_PRICE)

        assert trader._current_sl_price >= expected_be, (
            f"SL {trader._current_sl_price:.4f} is below break-even {expected_be:.4f}; "
            "taker fees are not fully covered"
        )

    def test_be_floor_holds_even_when_dynamic_sl_is_lower(
        self, tmp_path: pathlib.Path
    ) -> None:
        """When dynamic_sl < true_be, the SL must be clamped to true_be, not dynamic_sl."""
        fee_rate = 0.0004
        trader = _make_trader(tmp_path, fee_rate=fee_rate)
        _inject_long(trader, _ENTRY)

        true_be: float = _ENTRY + _ENTRY * (fee_rate * 2.0)

        trader._evaluate_exit(_ACTIVATION_PRICE)

        assert trader._current_sl_price >= true_be
