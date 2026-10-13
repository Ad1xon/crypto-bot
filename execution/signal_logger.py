"""Structured audit logger for trading signals and execution lifecycle events.

Appends atomic JSON-Lines records of all generated signals (both approved and
rejected with reason) and executed trades for post-hoc analysis and paper-vs-backtest
reconciliation.
"""

from __future__ import annotations

import json
import logging
import pathlib
import time
from dataclasses import asdict, dataclass
from typing import Any, Final

_LOG: Final[logging.Logger] = logging.getLogger(__name__)
_DEFAULT_LOG_PATH: Final[pathlib.Path] = pathlib.Path("data/signal_audit.jsonl")


@dataclass(frozen=True)
class SignalEvent:
    """Record of an evaluated microstructure signal event."""

    event_type: str  # "SIGNAL"
    timestamp_ms: int
    iso_time: str
    signal_type: str
    price: float
    vwap: float | None
    gex_value: float
    nearest_strike: float | None
    verdict: str  # "APPROVED" | "REJECTED"
    rejection_reason: str | None


@dataclass(frozen=True)
class TradeEvent:
    """Record of an executed trade entry or exit event."""

    event_type: str  # "TRADE_ENTRY" | "TRADE_EXIT"
    timestamp_ms: int
    iso_time: str
    direction: str
    entry_price: float
    exit_price: float | None = None
    size_btc: float = 0.0
    outcome: str | None = None  # "TP" | "SL" | "TIME_EXIT"
    net_pnl: float = 0.0
    fees: float = 0.0
    balance_after: float = 0.0


class SignalAuditLogger:
    """Non-blocking, file-backed audit logger for quant signals and executions."""

    def __init__(self, output_path: pathlib.Path = _DEFAULT_LOG_PATH) -> None:
        self.output_path: pathlib.Path = output_path
        self._ensure_parent_dir()

    def _ensure_parent_dir(self) -> None:
        try:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _LOG.warning("Failed to create audit log directory: %s", exc)

    def _append_record(self, record: dict[str, Any]) -> None:
        try:
            with open(self.output_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            _LOG.warning("Failed to append audit record to %s: %s", self.output_path, exc)

    def log_signal(
        self,
        timestamp_ms: int,
        signal_type: str,
        price: float,
        vwap: float | None,
        gex_value: float,
        nearest_strike: float | None,
        verdict: str,
        rejection_reason: str | None = None,
    ) -> SignalEvent:
        """Record a evaluated signal and its confluence gating outcome."""
        event = SignalEvent(
            event_type="SIGNAL",
            timestamp_ms=timestamp_ms,
            iso_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp_ms / 1000.0)),
            signal_type=signal_type,
            price=price,
            vwap=vwap,
            gex_value=gex_value,
            nearest_strike=nearest_strike,
            verdict=verdict,
            rejection_reason=rejection_reason,
        )
        self._append_record(asdict(event))
        return event

    def log_trade_entry(
        self,
        timestamp_ms: int,
        direction: str,
        entry_price: float,
        size_btc: float,
        balance_after: float,
    ) -> TradeEvent:
        """Record an opened trade position."""
        event = TradeEvent(
            event_type="TRADE_ENTRY",
            timestamp_ms=timestamp_ms,
            iso_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp_ms / 1000.0)),
            direction=direction,
            entry_price=entry_price,
            size_btc=size_btc,
            balance_after=balance_after,
        )
        self._append_record(asdict(event))
        return event

    def log_trade_exit(
        self,
        timestamp_ms: int,
        direction: str,
        entry_price: float,
        exit_price: float,
        size_btc: float,
        outcome: str,
        net_pnl: float,
        fees: float,
        balance_after: float,
    ) -> TradeEvent:
        """Record a closed trade position."""
        event = TradeEvent(
            event_type="TRADE_EXIT",
            timestamp_ms=timestamp_ms,
            iso_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp_ms / 1000.0)),
            direction=direction,
            entry_price=entry_price,
            exit_price=exit_price,
            size_btc=size_btc,
            outcome=outcome,
            net_pnl=net_pnl,
            fees=fees,
            balance_after=balance_after,
        )
        self._append_record(asdict(event))
        return event
