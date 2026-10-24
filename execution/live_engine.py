"""Real-time SMC signal engine with paper trading simulator.

``PaperTrader`` — simulated position manager with trailing SL, TP, fees, and
cooldown. ``LiveSignalEngine`` — rolling 150-tick deque fed through
MicrostructureDetector → SuperConfluenceFilter (VWAP + GEX) → ExecutionEngine.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import pathlib
import time
from typing import Any, Final

import pandas as pd

from execution.interfaces import ExecutionEngine
from execution.signal_logger import SignalAuditLogger
from quant_models.gex_risk_manager import GexRiskManager
from quant_models.macro_filter import MacroCalendarFilter
from quant_models.position_sizer import PositionSizer, cap_position_btc, gex_size_multiplier
from quant_models.sentiment_analyzer import CryptoSentimentAnalyzer
from quant_models.smc_detectors import MicrostructureDetector
from quant_models.super_confluence import (
    ConfluenceVerdict,
    SessionVwap,
    SuperConfluenceFilter,
)

_LOG: Final[logging.Logger] = logging.getLogger(__name__)
_DEQUE_MAXLEN: Final[int] = 150
_MIN_TICKS: Final[int] = 110
_BULLISH: Final[str] = "BULLISH_ABSORPTION"
_BEARISH: Final[str] = "BEARISH_ABSORPTION"


class PaperTrader(ExecutionEngine):
    """Simulated position manager for live paper trading.

    Mirrors the BacktestEngine lifecycle: taker fees on both legs, high-water-mark
    trailing stop-to-break-even, static TP, and a post-exit cooldown.
    """

    def __init__(
        self,
        balance: float = 10_000.0,
        position_size_usdt: float = 1_000.0,
        tp_usdt: float = 300.0,
        sl_usdt: float = 150.0,
        trailing_activation_usdt: float = 100.0,
        tp_pct: float | None = None,
        sl_pct: float | None = None,
        trailing_activation_pct: float | None = None,
        fee_rate: float = 0.0004,
        cooldown_s: float = 60.0,
        slippage_bps: float = 2.0,
        max_daily_loss_usdt: float = 500.0,
        position_sizer: PositionSizer | None = None,
        max_order_size_btc: float | None = None,
        max_leverage: float | None = None,
        audit_logger: SignalAuditLogger | None = None,
        state_file: pathlib.Path = pathlib.Path("data/paper_state.json"),
    ) -> None:
        self._balance: float = balance
        self._position_size_usdt: float = position_size_usdt
        self._tp_usdt: float = tp_usdt
        self._sl_usdt: float = sl_usdt
        self._trailing_activation_usdt: float = trailing_activation_usdt
        self._tp_pct: float | None = tp_pct
        self._sl_pct: float | None = sl_pct
        self._trailing_activation_pct: float | None = trailing_activation_pct
        self._active_tp_dist: float = tp_usdt
        self._active_sl_dist: float = sl_usdt
        self._active_activation_dist: float = trailing_activation_usdt
        self._fee_rate: float = fee_rate
        self._cooldown_s: float = cooldown_s
        self._slippage_bps: float = slippage_bps
        self._max_daily_loss_usdt: float = max_daily_loss_usdt
        self._position_sizer: PositionSizer | None = position_sizer
        self._max_order_size_btc: float | None = max_order_size_btc
        self._max_leverage: float | None = max_leverage
        self._audit_logger: SignalAuditLogger | None = audit_logger

        self._position: str | None = None
        self._entry_price: float = 0.0
        self._position_btc: float = 0.0
        self._entry_fee: float = 0.0
        self._current_sl_price: float = 0.0
        self._last_trade_time: float = 0.0
        self._session_pnl: float = 0.0

        self.state_file: pathlib.Path = state_file
        self._load_state()

    @property
    def balance(self) -> float:
        return self._balance

    async def fetch_balance(self) -> float:
        return self._balance

    @property
    def has_open_position(self) -> bool:
        return self._position is not None

    def is_on_cooldown(self, current_time: float) -> bool:
        return current_time - self._last_trade_time < self._cooldown_s

    def _build_state_dict(self) -> dict[str, Any]:
        if self._position == "LONG":
            tp_price: float = self._entry_price + self._active_tp_dist
        elif self._position == "SHORT":
            tp_price = self._entry_price - self._active_tp_dist
        else:
            tp_price = 0.0

        return {
            "balance": self._balance,
            "position": self._position,
            "entry_price": self._entry_price,
            "position_btc": self._position_btc,
            "entry_fee": self._entry_fee,
            "current_sl_price": self._current_sl_price,
            "last_trade_time": self._last_trade_time,
            "session_pnl": self._session_pnl,
            "tp_price": tp_price,
        }

    def _save_state(self) -> None:
        """Write state atomically via tmp-then-rename."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp: pathlib.Path = self.state_file.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._build_state_dict(), fh, indent=2)
            tmp.replace(self.state_file)
        except OSError as exc:
            _LOG.error("PaperTrader: failed to persist state — %s", exc)

    def _load_state(self) -> None:
        """Restore state from disk; falls back to defaults on any error."""
        if not self.state_file.exists():
            self._save_state()
            _LOG.info(
                "[PAPER TRADER] No state file found — starting fresh with balance $%s.",
                f"{self._balance:,.2f}",
            )
            return

        try:
            with open(self.state_file, "r", encoding="utf-8") as fh:
                data: dict[str, Any] = json.load(fh)

            self._balance = float(data["balance"])
            raw_position = data.get("position")
            self._position = str(raw_position) if raw_position is not None else None
            self._entry_price = float(data.get("entry_price", 0.0))
            self._position_btc = float(data.get("position_btc", 0.0))
            self._entry_fee = float(data.get("entry_fee", 0.0))
            self._current_sl_price = float(data.get("current_sl_price", 0.0))
            self._last_trade_time = float(data.get("last_trade_time", 0.0))
            self._session_pnl = float(data.get("session_pnl", 0.0))

            _LOG.warning(
                "[PAPER TRADER] Restored state from disk. "
                "Balance: $%s | Position: %s | Entry: $%s | SL: $%s | Session PnL: $%s",
                f"{self._balance:,.2f}",
                self._position if self._position is not None else "None",
                f"{self._entry_price:,.2f}",
                f"{self._current_sl_price:,.2f}",
                f"{self._session_pnl:+,.4f}",
            )

        except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
            _LOG.warning(
                "[PAPER TRADER] State file unreadable (%s) — starting from defaults.",
                exc,
            )
            self._save_state()

    async def update_tick(self, current_price: float, current_time: float) -> None:
        """Check TP/SL on the open position and close it if breached."""
        if self._position is None:
            return

        exit_result: tuple[str, float] | None = self._evaluate_exit(current_price)
        if exit_result is not None:
            self._close_position(*exit_result, current_time)
            self._save_state()

    async def execute_signal(
        self,
        signal_type: str,
        current_price: float,
        current_time: float,
        gex_value: float = 0.0,
        signal_ts: float | None = None,
        sentiment_score: float = 0.0,
    ) -> None:
        """Open a new position for the signal, subject to kill-switch and cooldown guards."""
        if self._session_pnl <= -self._max_daily_loss_usdt:
            _LOG.critical(
                "Kill-Switch active: Session loss limit reached "
                "(Session PnL: $%s / Limit: -$%.2f) — signal %s BLOCKED.",
                f"{self._session_pnl:+,.4f}",
                self._max_daily_loss_usdt,
                signal_type,
            )
            return

        if current_time - self._last_trade_time < self._cooldown_s:
            remaining: float = self._cooldown_s - (current_time - self._last_trade_time)
            _LOG.debug(
                "PaperTrader cooldown active — %.1fs remaining, signal %s ignored.",
                remaining,
                signal_type,
            )
            return

        if self._position is not None:
            _LOG.debug(
                "PaperTrader already in a %s position — signal %s ignored.",
                self._position,
                signal_type,
            )
            return

        self._open_position(signal_type, current_price, gex_value=gex_value, sentiment_score=sentiment_score)
        self._save_state()
        if signal_ts is not None:
            fill_ts: float = time.perf_counter()
            latency_ms: float = (fill_ts - signal_ts) * 1000.0
            _LOG.info("Order executed. Latency: %.2f ms", latency_ms)

    def _open_position(
        self,
        signal_type: str,
        price: float,
        gex_value: float = 0.0,
        sentiment_score: float = 0.0,
    ) -> None:
        """Record entry, apply slippage, and deduct the entry taker fee."""
        self._position = "LONG" if signal_type == _BULLISH else "SHORT"

        slip_factor: float = self._slippage_bps / 10_000.0
        if self._position == "LONG":
            self._entry_price = price * (1.0 + slip_factor)
        else:
            self._entry_price = price * (1.0 - slip_factor)

        if self._sl_pct is not None:
            self._active_sl_dist = self._entry_price * self._sl_pct
            self._active_tp_dist = self._entry_price * (
                self._tp_pct if self._tp_pct is not None else self._sl_pct * 2.0
            )
            self._active_activation_dist = (
                self._entry_price * self._trailing_activation_pct
                if self._trailing_activation_pct is not None
                else self._active_tp_dist / 3.0
            )
        else:
            self._active_sl_dist = self._sl_usdt
            self._active_tp_dist = self._tp_usdt
            self._active_activation_dist = self._trailing_activation_usdt

        if self._position_sizer is not None:
            raw_size_btc: float = self._position_sizer.calculate_position_btc(
                account_balance_usdt=self._balance,
                sl_distance_usdt=self._active_sl_dist,
                gex_modifier=gex_size_multiplier(gex_value),
                sentiment_score=sentiment_score,
                signal_type=signal_type,
            )
            self._position_btc = cap_position_btc(
                raw_size_btc,
                self._entry_price,
                self._balance,
                self._max_order_size_btc,
                self._max_leverage,
            )
            actual_size_usdt: float = self._position_btc * self._entry_price
        else:
            actual_size_usdt = self._position_size_usdt
            self._position_btc = actual_size_usdt / self._entry_price

        self._entry_fee = self._fee_rate * actual_size_usdt
        self._balance -= self._entry_fee

        if self._position == "LONG":
            self._current_sl_price = self._entry_price - self._active_sl_dist
        else:
            self._current_sl_price = self._entry_price + self._active_sl_dist

        tp_level: float = (
            self._entry_price + self._active_tp_dist
            if self._position == "LONG"
            else self._entry_price - self._active_tp_dist
        )
        _LOG.warning(
            "[PAPER TRADE ENTRY] Opened %s at $%s (mkt $%s, slip %.1fbps) | "
            "Size: $%.0f (%.6f BTC) | TP: $%s | SL: $%s | Balance: $%s",
            self._position,
            f"{self._entry_price:,.2f}",
            f"{price:,.2f}",
            self._slippage_bps,
            actual_size_usdt,
            self._position_btc,
            f"{tp_level:,.2f}",
            f"{self._current_sl_price:,.2f}",
            f"{self._balance:,.2f}",
        )
        if self._audit_logger is not None and self._position is not None:
            self._audit_logger.log_trade_entry(
                timestamp_ms=int(time.time() * 1000),
                direction=self._position,
                entry_price=self._entry_price,
                size_btc=self._position_btc,
                balance_after=self._balance,
            )

    def _evaluate_exit(self, price: float) -> tuple[str, float] | None:
        """Return ``(outcome, fill_price)`` if any TP/SL level is breached, else None.

        The trailing SL ratchets monotonically once profit clears
        ``trailing_activation_usdt``, with the fee-aware break-even as a floor.
        Evaluation order: trailing update → TP → SL.
        """
        fee_coverage: float = self._entry_price * (self._fee_rate * 2.0)
        sl_price_delta: float = (
            self._active_sl_dist
            if self._sl_pct is not None or self._position_btc == 0.0
            else self._active_sl_dist / self._position_btc
        )
        activation_price_delta: float = (
            self._active_activation_dist
            if self._trailing_activation_pct is not None or self._sl_pct is not None or self._position_btc == 0.0
            else self._active_activation_dist / self._position_btc
        )

        if self._position == "LONG":
            true_be_price: float = self._entry_price + fee_coverage
            if price >= self._entry_price + activation_price_delta:
                dynamic_sl: float = price - sl_price_delta
                self._current_sl_price = max(self._current_sl_price, true_be_price, dynamic_sl)
            if price >= self._entry_price + self._active_tp_dist:
                return ("TP", self._entry_price + self._active_tp_dist)
            if price <= self._current_sl_price:
                return ("SL", self._current_sl_price)

        elif self._position == "SHORT":
            true_be_price = self._entry_price - fee_coverage
            if price <= self._entry_price - activation_price_delta:
                dynamic_sl = price + sl_price_delta
                self._current_sl_price = min(self._current_sl_price, true_be_price, dynamic_sl)
            if price <= self._entry_price - self._active_tp_dist:
                return ("TP", self._entry_price - self._active_tp_dist)
            if price >= self._current_sl_price:
                return ("SL", self._current_sl_price)

        return None

    def _close_position(self, outcome: str, exit_price: float, current_time: float) -> None:
        """Settle PnL, update balance, log the result, and reset position state."""
        exit_notional: float = self._position_btc * exit_price
        exit_fee: float = self._fee_rate * exit_notional
        total_fees: float = self._entry_fee + exit_fee

        gross_pnl: float = (
            self._position_btc * (exit_price - self._entry_price)
            if self._position == "LONG"
            else self._position_btc * (self._entry_price - exit_price)
        )
        net_pnl: float = gross_pnl - total_fees
        self._balance += gross_pnl - exit_fee
        self._session_pnl += net_pnl

        pnl_sign: str = "+" if net_pnl >= 0.0 else ""
        _LOG.warning(
            "[PAPER TRADE EXIT] Result: %s | Direction: %s | "
            "Entry: $%s → Exit: $%s | "
            "Gross PnL: %s$%.4f | Fees: $%.4f | "
            "Net PnL: %s$%.4f | New Balance: $%s",
            outcome,
            self._position,
            f"{self._entry_price:,.2f}",
            f"{exit_price:,.2f}",
            pnl_sign,
            gross_pnl,
            total_fees,
            pnl_sign,
            net_pnl,
            f"{self._balance:,.4f}",
        )

        if self._audit_logger is not None and self._position is not None:
            self._audit_logger.log_trade_exit(
                timestamp_ms=int(current_time * 1000),
                direction=self._position,
                entry_price=self._entry_price,
                exit_price=exit_price,
                size_btc=self._position_btc,
                outcome=outcome,
                net_pnl=net_pnl,
                fees=total_fees,
                balance_after=self._balance,
            )

        self._last_trade_time = current_time
        self._position = None
        self._entry_price = 0.0
        self._position_btc = 0.0
        self._entry_fee = 0.0
        self._current_sl_price = 0.0


class LiveSignalEngine:
    """Rolling-window SMC evaluator with pluggable async execution engine.

    Dispatches ``update_tick`` and ``execute_signal`` as ``asyncio.Task``
    coroutines so the WebSocket receive loop is never blocked by REST I/O.
    Entry requires SuperConfluenceFilter approval (VWAP + nearest-strike GEX).
    """

    def __init__(
        self,
        detector: MicrostructureDetector | None = None,
        gex_manager: GexRiskManager | None = None,
        confluence: SuperConfluenceFilter | None = None,
        session_vwap: SessionVwap | None = None,
        execution_engine: ExecutionEngine | None = None,
        macro_filter: MacroCalendarFilter | None = None,
        sentiment_analyzer: CryptoSentimentAnalyzer | None = None,
        audit_logger: SignalAuditLogger | None = None,
    ) -> None:
        self._detector: MicrostructureDetector = (
            detector if detector is not None else MicrostructureDetector(divergence_window=100)
        )
        self._gex: GexRiskManager = gex_manager if gex_manager is not None else GexRiskManager()
        self._confluence: SuperConfluenceFilter = confluence if confluence is not None else SuperConfluenceFilter()
        self._session_vwap: SessionVwap = session_vwap if session_vwap is not None else SessionVwap()
        self.execution_engine: ExecutionEngine = execution_engine if execution_engine is not None else PaperTrader()
        self._macro_filter: MacroCalendarFilter | None = macro_filter
        self._sentiment_analyzer: CryptoSentimentAnalyzer | None = sentiment_analyzer
        self._audit_logger: SignalAuditLogger | None = audit_logger
        self._window: collections.deque[dict] = collections.deque(maxlen=_DEQUE_MAXLEN)
        self._tick_task: asyncio.Task[None] | None = None
        self._signal_task: asyncio.Task[None] | None = None

    def clear_window(self) -> None:
        """Clear rolling tick window to prevent cross-disconnection false signals."""
        _LOG.info("[LIVE SIGNAL ENGINE] Clearing rolling window after disconnection.")
        self._window.clear()

    def update_trade(self, price: float, qty: float) -> None:
        """Fold a public trade into the shared session VWAP accumulator."""
        self._session_vwap.update(price, qty)

    def process_tick(
        self,
        timestamp_ms: int,
        price: float,
        cvd: float,
        ofi: float,
    ) -> None:
        """Ingest one BBO tick, schedule the SL monitor, and run the signal pipeline.

        ``update_tick`` is scheduled each tick unless the previous task is still
        in flight (e.g. an exit order is pending). ``execute_signal`` is only
        scheduled when a new approved signal is detected and no prior entry task
        is running.
        """
        current_time: float = time.time()

        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.create_task(self.execution_engine.update_tick(price, current_time))

        self._window.append(
            {
                "timestamp_ms": timestamp_ms,
                "price": price,
                "cvd": cvd,
                "ofi": ofi,
            }
        )

        if self._signal_task is not None and not self._signal_task.done():
            return

        if self.execution_engine.has_open_position:
            return

        if self.execution_engine.is_on_cooldown(current_time):
            return

        if len(self._window) < _MIN_TICKS:
            return

        df: pd.DataFrame = self._build_frame()
        df = self._detector.smooth_ofi(df)
        signals: pd.DataFrame = self._detector.detect_cvd_divergence(df)

        if signals.empty:
            return

        last_signal_ts: int = int(signals.iloc[-1]["timestamp_ms"])
        if last_signal_ts != timestamp_ms:
            return

        signal_type: str = str(signals.iloc[-1]["signal"])
        signal_price: float = float(signals.iloc[-1]["price"])
        signal_ts: float = float(signals.iloc[-1]["signal_ts"])
        verdict: ConfluenceVerdict = self._confluence.evaluate(
            signal_type=signal_type,
            price=signal_price,
            vwap=self._session_vwap.value,
            gex_map=self._gex.get_gex_map(),
        )
        status: str = "APPROVED" if verdict.allowed else "REJECTED"

        if verdict.nearest_strike is not None:
            _LOG.warning(
                "[LIVE SIGNAL] %s at $%.2f | Super-Confluence: %s "
                "(VWAP ok=%s | GEX ok=%s | Nearest Strike: %.0f, GEX: %.6f | VWAP: %s)",
                signal_type,
                signal_price,
                status,
                verdict.vwap_ok,
                verdict.gex_ok,
                verdict.nearest_strike,
                verdict.gex_value,
                f"${self._session_vwap.value:,.2f}" if self._session_vwap.value is not None else "n/a",
            )
        else:
            _LOG.warning(
                "[LIVE SIGNAL] %s at $%.2f | Super-Confluence: %s "
                "(VWAP ok=%s | GEX ok=%s | no GEX snapshot — Deribit may be offline)",
                signal_type,
                signal_price,
                status,
                verdict.vwap_ok,
                verdict.gex_ok,
            )

        rejection_reason: str | None = None
        if not verdict.allowed:
            rejection_reason = "CONFLUENCE_FILTER"
        elif self._macro_filter is not None and not self._macro_filter.is_trading_allowed(timestamp_ms):
            rejection_reason = "MACRO_BLACKOUT"

        if self._audit_logger is not None:
            self._audit_logger.log_signal(
                timestamp_ms=timestamp_ms,
                signal_type=signal_type,
                price=signal_price,
                vwap=self._session_vwap.value,
                gex_value=verdict.gex_value,
                nearest_strike=verdict.nearest_strike,
                verdict="APPROVED" if rejection_reason is None else "REJECTED",
                rejection_reason=rejection_reason,
            )

        if rejection_reason == "MACRO_BLACKOUT":
            _LOG.warning(
                "[LIVE SIGNAL] %s at $%.2f REJECTED by MacroCalendarFilter (within high-impact blackout window).",
                signal_type,
                signal_price,
            )
            return

        if verdict.allowed:
            sentiment_score: float = (
                self._sentiment_analyzer.current_score.net_score if self._sentiment_analyzer is not None else 0.0
            )
            self._signal_task = asyncio.create_task(
                self.execution_engine.execute_signal(
                    signal_type,
                    signal_price,
                    current_time,
                    verdict.gex_value,
                    signal_ts,
                    sentiment_score,
                )
            )

    def _build_frame(self) -> pd.DataFrame:
        """Materialise the deque as a UTC-timestamped DataFrame for the detector."""
        df: pd.DataFrame = pd.DataFrame(self._window)
        df["dt"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
        return df
