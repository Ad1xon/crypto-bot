"""Event-driven tick-level backtester for SMC absorption signals.

Replays the merged BBO + Trade Parquet data tick by tick, applying the
MicrostructureDetector to generate BULLISH_ABSORPTION and BEARISH_ABSORPTION
entry signals, then gating entries through the shared SuperConfluenceFilter
(VWAP + nearest-strike GEX) used by the live engine.

Anti-look-ahead guarantees
--------------------------
- Signals use ``Series.diff(window)`` which references only past ticks.
- On each tick the exit check runs *before* the entry check, preventing
  same-bar open-and-close fills.
- Entry and exit prices are the exact tick prices at the moment of trigger —
  no future bar's open is used.

Usage (from project root)
--------------------------
    python research/event_backtester.py --symbol btc
    python research/event_backtester.py --symbol btc --tp-pct 0.004 --sl-pct 0.002
    python research/event_backtester.py --gex-file data/live_gex.json   # smoke test only
"""

from __future__ import annotations

import argparse
import math
import pathlib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from config.settings import SETTINGS
from quant_models.gex_risk_manager import GexHistory, GexRiskManager, load_gex_map_from_file
from quant_models.position_sizer import PositionSizer, cap_position_btc, gex_size_multiplier
from quant_models.smc_detectors import MicrostructureDetector
from quant_models.super_confluence import SuperConfluenceFilter
from research.data_loader import load_merged_frame

_CONSOLE: Final[Console] = Console()
_DATA_DIR: Final[pathlib.Path] = pathlib.Path("data")
_DEFAULT_GEX_HISTORY_DIR: Final[pathlib.Path] = _DATA_DIR / "gex_history"
MIN_TRADES_FOR_SIGNIFICANCE: Final[int] = 30
_SAMPLE_OK_TRADES: Final[int] = 100


@dataclass(frozen=True)
class TradeConfig:
    """Immutable configuration for the backtesting engine."""

    initial_balance: float = 10_000.0
    position_size_usdt: float = 1_000.0
    take_profit_usdt: float = 300.0
    stop_loss_usdt: float = 150.0
    tp_pct: float | None = None
    sl_pct: float | None = None
    fee_rate: float = 0.00045
    slippage_bps: float = 0.0
    cooldown_ms: int = 60_000
    trailing_activation_usdt: float = 100.0
    trailing_activation_pct: float | None = None
    max_duration_s: int = 1800
    max_order_size_btc: float | None = None
    max_leverage: float | None = None


@dataclass(frozen=True)
class TradeRecord:
    """Immutable record of a single completed round-trip trade."""

    trade_id: int
    direction: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    position_btc: float
    gross_pnl: float
    total_fees: float
    net_pnl: float
    outcome: str
    duration_s: float
    balance_after: float


@dataclass(frozen=True)
class PerformanceReport:
    """Aggregate performance metrics computed from a completed backtest."""

    initial_balance: float
    final_balance: float
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate_pct: float
    gross_profit: float
    gross_loss: float
    profit_factor: float
    net_pnl: float
    return_pct: float
    max_drawdown_usdt: float
    max_drawdown_pct: float
    avg_win_usdt: float
    avg_loss_usdt: float
    avg_duration_s: float
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0
    win_loss_ratio: float = 0.0


class _TickRow(Protocol):
    """Structural type for rows produced by ``DataFrame.itertuples``."""

    timestamp_ms: int
    price: float
    vwap: float
    dt: pd.Timestamp


class BacktestEngine:
    """Event-driven state machine that replays tick data and manages positions.

    Responsibilities:
    - Accept tick events one at a time via :meth:`process_tick`.
    - Check active positions for TP / SL breach before evaluating entry signals.
    - Gate entries with the shared :class:`SuperConfluenceFilter` (VWAP + GEX).
    - Enforce a post-trade cooldown window to prevent cluster re-entry.
    - Apply taker fees on both entry and exit legs.
    - Maintain a complete trade log and running equity curve for analysis.
    """

    def __init__(
        self,
        config: TradeConfig,
        signal_map: dict[int, str],
        gex_manager: GexRiskManager | None = None,
        confluence: SuperConfluenceFilter | None = None,
        position_sizer: PositionSizer | None = None,
        gex_history: GexHistory | None = None,
    ) -> None:
        self._config: TradeConfig = config
        self._signal_map: dict[int, str] = signal_map
        self._gex: GexRiskManager | None = gex_manager
        self._gex_history: GexHistory | None = gex_history
        self._position_sizer: PositionSizer | None = position_sizer
        self._confluence: SuperConfluenceFilter = confluence if confluence is not None else SuperConfluenceFilter()

        self._balance: float = config.initial_balance
        self._position: str | None = None
        self._entry_price: float = 0.0
        self._entry_time: pd.Timestamp | None = None
        self._position_btc: float = 0.0
        self._entry_fee: float = 0.0
        self._current_sl_price: float = 0.0
        self._cooldown_until_ms: int = 0
        self._next_trade_id: int = 1

        self._active_tp_dist: float = config.take_profit_usdt
        self._active_sl_dist: float = config.stop_loss_usdt
        self._active_activation: float = config.trailing_activation_usdt

        self._trade_records: list[TradeRecord] = []
        self._equity_curve: list[float] = [config.initial_balance]

    def process_tick(self, row: _TickRow) -> None:
        """Advance the state machine by one 100 ms tick.

        Exit evaluation is intentionally performed before entry evaluation so
        that a tick whose price crosses both a TP level and a new signal
        timestamp always closes the open trade first — preventing any
        same-bar open-and-immediately-close artefact.
        """
        if not pd.isna(row.price) and self._position is not None:
            exit_info = self._check_exit(row.price, row.dt)
            if exit_info is not None:
                self._close_position(*exit_info, row.dt, row.timestamp_ms)

        if (
            self._position is None
            and row.timestamp_ms in self._signal_map
            and row.timestamp_ms >= self._cooldown_until_ms
        ):
            signal: str = self._signal_map[row.timestamp_ms]
            vwap: float | None = None if pd.isna(row.vwap) else float(row.vwap)
            gex_map: dict[float, float] = (
                self._gex_history.map_at(row.timestamp_ms)
                if self._gex_history is not None
                else (self._gex.get_gex_map() if self._gex is not None else {})
            )
            verdict = self._confluence.evaluate(
                signal_type=signal,
                price=float(row.price),
                vwap=vwap,
                gex_map=gex_map,
            )
            if verdict.allowed:
                self._open_position(signal, row.price, row.dt, gex_value=verdict.gex_value)

    def _check_exit(self, price: float, dt: pd.Timestamp) -> tuple[str, float] | None:
        """Return ``(outcome, fill_price)`` if any exit condition is met.

        Evaluation order: time-based exit → trailing stop update → TP → SL.
        TP fill price is clamped to the exact level; TIME_EXIT and trailing-SL
        fills use the live tick price / current SL price respectively.
        """
        elapsed_s: float = (dt - self._entry_time).total_seconds()  # type: ignore[operator]
        if elapsed_s >= self._config.max_duration_s:
            return ("TIME_EXIT", price)

        tp_dist: float = self._active_tp_dist
        activation: float = self._active_activation

        if self._position == "LONG":
            if price >= self._entry_price + activation:
                self._current_sl_price = max(self._current_sl_price, self._entry_price)
            if price >= self._entry_price + tp_dist:
                return ("TP", self._entry_price + tp_dist)
            if price <= self._current_sl_price:
                return ("SL", self._current_sl_price)
        elif self._position == "SHORT":
            if price <= self._entry_price - activation:
                self._current_sl_price = min(self._current_sl_price, self._entry_price)
            if price <= self._entry_price - tp_dist:
                return ("TP", self._entry_price - tp_dist)
            if price >= self._current_sl_price:
                return ("SL", self._current_sl_price)
        return None

    def _open_position(self, signal: str, price: float, dt: pd.Timestamp, gex_value: float = 0.0) -> None:
        """Record entry with adverse slippage, size the position and deduct the entry taker fee."""
        self._position = "LONG" if signal == "BULLISH_ABSORPTION" else "SHORT"
        slip: float = self._config.slippage_bps / 10_000.0
        self._entry_price = price * (1.0 + slip) if self._position == "LONG" else price * (1.0 - slip)
        self._entry_time = dt
        self._entry_fee = 0.0
        self._position_btc = 0.0

        if self._config.sl_pct is not None:
            self._active_sl_dist = self._entry_price * self._config.sl_pct
            self._active_tp_dist = self._entry_price * (
                self._config.tp_pct if self._config.tp_pct is not None else self._config.sl_pct * 2.0
            )
            self._active_activation = (
                self._entry_price * self._config.trailing_activation_pct
                if self._config.trailing_activation_pct is not None
                else self._active_tp_dist / 3.0
            )
        else:
            self._active_sl_dist = self._config.stop_loss_usdt
            self._active_tp_dist = self._config.take_profit_usdt
            self._active_activation = self._config.trailing_activation_usdt

        if self._position == "LONG":
            self._current_sl_price = self._entry_price - self._active_sl_dist
        else:
            self._current_sl_price = self._entry_price + self._active_sl_dist

        if self._position_sizer is not None:
            raw_size_btc: float = self._position_sizer.calculate_position_btc(
                account_balance_usdt=self._balance,
                sl_distance_usdt=self._active_sl_dist,
                gex_modifier=gex_size_multiplier(gex_value),
            )
            size_btc: float = cap_position_btc(
                raw_size_btc,
                self._entry_price,
                self._balance,
                self._config.max_order_size_btc,
                self._config.max_leverage,
            )
        else:
            size_btc = self._config.position_size_usdt / self._entry_price

        if size_btc <= 0.0:
            self._position = None
            return

        self._position_btc = size_btc
        self._entry_fee = self._config.fee_rate * size_btc * self._entry_price
        self._balance -= self._entry_fee

    def _close_position(
        self,
        outcome: str,
        exit_price: float,
        dt: pd.Timestamp,
        ts_ms: int,
    ) -> None:
        """Settle the trade, record it, update balance, and start cooldown.

        TP fills at the resting limit level; SL and TIME_EXIT are market exits and pay
        adverse slippage. ``net_pnl`` includes the entry fee already deducted from balance.
        """
        if outcome != "TP":
            slip: float = self._config.slippage_bps / 10_000.0
            exit_price *= (1.0 - slip) if self._position == "LONG" else (1.0 + slip)
        exit_notional: float = self._position_btc * exit_price
        exit_fee: float = self._config.fee_rate * exit_notional
        total_fees: float = self._entry_fee + exit_fee

        gross_pnl: float = (
            self._position_btc * (exit_price - self._entry_price)
            if self._position == "LONG"
            else self._position_btc * (self._entry_price - exit_price)
        )
        net_pnl: float = gross_pnl - total_fees
        self._balance += gross_pnl - exit_fee

        self._trade_records.append(
            TradeRecord(
                trade_id=self._next_trade_id,
                direction=self._position,  # type: ignore[arg-type]
                entry_time=self._entry_time,  # type: ignore[arg-type]
                exit_time=dt,
                entry_price=self._entry_price,
                exit_price=exit_price,
                position_btc=self._position_btc,
                gross_pnl=gross_pnl,
                total_fees=total_fees,
                net_pnl=net_pnl,
                outcome=outcome,
                duration_s=(dt - self._entry_time).total_seconds(),  # type: ignore[operator]
                balance_after=self._balance,
            )
        )
        self._equity_curve.append(self._balance)
        self._next_trade_id += 1
        self._cooldown_until_ms = ts_ms + self._config.cooldown_ms
        self._position = None

    @property
    def trade_records(self) -> list[TradeRecord]:
        """Return a snapshot of all completed trade records."""
        return list(self._trade_records)

    @property
    def equity_curve(self) -> list[float]:
        """Return the balance at each trade close, starting with initial balance."""
        return list(self._equity_curve)

    @property
    def has_open_position(self) -> bool:
        """True if a position is still open at end of data."""
        return self._position is not None


def sample_size_label(total_trades: int) -> str:
    """Classify how much statistical weight a trade count carries."""
    if total_trades < MIN_TRADES_FOR_SIGNIFICANCE:
        return "INSUFFICIENT"
    if total_trades < _SAMPLE_OK_TRADES:
        return "LOW"
    return "OK"


def compute_performance_report(
    records: list[TradeRecord],
    equity_curve: list[float],
    config: TradeConfig,
) -> PerformanceReport:
    """Compute aggregate performance metrics from a list of trade records.

    Max drawdown is computed vectorised via ``cummax`` on the equity curve
    Series — no Python-level loop over the equity history.
    """
    if not records:
        return PerformanceReport(
            initial_balance=config.initial_balance,
            final_balance=config.initial_balance,
            total_trades=0,
            winning_trades=0,
            losing_trades=0,
            win_rate_pct=0.0,
            gross_profit=0.0,
            gross_loss=0.0,
            profit_factor=0.0,
            net_pnl=0.0,
            return_pct=0.0,
            max_drawdown_usdt=0.0,
            max_drawdown_pct=0.0,
            avg_win_usdt=0.0,
            avg_loss_usdt=0.0,
            avg_duration_s=0.0,
        )

    wins: list[TradeRecord] = [r for r in records if r.net_pnl > 0]
    losses: list[TradeRecord] = [r for r in records if r.net_pnl <= 0]

    gross_profit: float = sum(r.net_pnl for r in wins)
    gross_loss: float = abs(sum(r.net_pnl for r in losses))
    profit_factor: float = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    equity: pd.Series = pd.Series(equity_curve, dtype=float)
    rolling_peak: pd.Series = equity.cummax()
    drawdown_usdt: pd.Series = rolling_peak - equity
    drawdown_pct: pd.Series = (drawdown_usdt / rolling_peak) * 100

    net_pnl: float = sum(r.net_pnl for r in records)
    final_balance: float = equity_curve[-1]
    return_pct: float = net_pnl / config.initial_balance * 100
    max_drawdown_usdt: float = float(drawdown_usdt.max())
    max_drawdown_pct: float = float(drawdown_pct.max())
    avg_win: float = gross_profit / len(wins) if wins else 0.0
    avg_loss: float = -(gross_loss / len(losses)) if losses else 0.0

    net_pnls: list[float] = [r.net_pnl for r in records]
    avg_pnl: float = net_pnl / len(records)

    # Sharpe ratio (per-trade return / std of returns)
    if len(records) > 1:
        variance: float = sum((p - avg_pnl) ** 2 for p in net_pnls) / (len(records) - 1)
        std_pnl: float = math.sqrt(variance)
        sharpe_ratio: float = avg_pnl / std_pnl if std_pnl > 0 else 0.0
    else:
        sharpe_ratio = 0.0

    # Sortino ratio (trade returns penalising only downside variance)
    neg_pnls: list[float] = [p for p in net_pnls if p < 0]
    if neg_pnls:
        downside_std: float = math.sqrt(sum(p**2 for p in neg_pnls) / len(records))
        sortino_ratio: float = avg_pnl / downside_std if downside_std > 0 else 0.0
    elif avg_pnl > 0:
        sortino_ratio = float("inf")
    else:
        sortino_ratio = 0.0

    # Calmar ratio (return_pct / max_drawdown_pct)
    if max_drawdown_pct > 0:
        calmar_ratio: float = return_pct / max_drawdown_pct
    elif return_pct > 0:
        calmar_ratio = float("inf")
    else:
        calmar_ratio = 0.0

    # Win / Loss ratio (payoff ratio: average win vs average loss)
    if avg_loss < 0:
        win_loss_ratio: float = abs(avg_win / avg_loss)
    elif avg_win > 0:
        win_loss_ratio = float("inf")
    else:
        win_loss_ratio = 0.0

    return PerformanceReport(
        initial_balance=config.initial_balance,
        final_balance=final_balance,
        total_trades=len(records),
        winning_trades=len(wins),
        losing_trades=len(losses),
        win_rate_pct=len(wins) / len(records) * 100,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        profit_factor=profit_factor,
        net_pnl=net_pnl,
        return_pct=return_pct,
        max_drawdown_usdt=max_drawdown_usdt,
        max_drawdown_pct=max_drawdown_pct,
        avg_win_usdt=avg_win,
        avg_loss_usdt=avg_loss,
        avg_duration_s=sum(r.duration_s for r in records) / len(records),
        sharpe_ratio=sharpe_ratio,
        sortino_ratio=sortino_ratio,
        calmar_ratio=calmar_ratio,
        win_loss_ratio=win_loss_ratio,
    )


def _render_trade_log(records: list[TradeRecord], config: TradeConfig) -> None:
    """Print a Rich Table with one row per completed trade."""
    levels: str = (
        f"TP={config.tp_pct * 100:.2f}%  SL={config.sl_pct * 100:.2f}%"
        if config.sl_pct is not None and config.tp_pct is not None
        else f"TP={config.take_profit_usdt:.0f}  SL={config.stop_loss_usdt:.0f}"
    )
    table = Table(
        title=f"Trade Log  ({len(records)} trades  |  fee={config.fee_rate * 100:.3f}%  "
        f"slip={config.slippage_bps:.1f}bps  {levels})",
        box=box.ROUNDED,
        header_style="bold white",
        border_style="bright_blue",
        show_lines=False,
    )
    table.add_column("#", justify="right", style="dim", min_width=3)
    table.add_column("Dir", justify="center", min_width=6)
    table.add_column("Entry Time", justify="left", style="cyan", min_width=22)
    table.add_column("Entry $", justify="right", min_width=12)
    table.add_column("Exit $", justify="right", min_width=12)
    table.add_column("Gross PnL", justify="right", min_width=11)
    table.add_column("Fees", justify="right", style="dim yellow", min_width=8)
    table.add_column("Net PnL", justify="right", min_width=11)
    table.add_column("Result", justify="center", min_width=4)
    table.add_column("Duration", justify="right", style="dim", min_width=10)
    table.add_column("Balance", justify="right", min_width=12)

    for r in records:
        is_win: bool = r.net_pnl > 0
        pnl_color: str = "bold green" if r.net_pnl >= 0 else "bold red"
        res_color: str = "bold yellow" if r.outcome == "TIME_EXIT" else "bold green" if is_win else "bold red"
        dir_color: str = "green" if r.direction == "LONG" else "red"
        gpnl_sign: str = "+" if r.gross_pnl >= 0 else ""
        npnl_sign: str = "+" if r.net_pnl >= 0 else ""
        entry_ts: str = str(r.entry_time).replace("+00:00", "").strip()
        duration_str: str = f"{r.duration_s:.0f}s" if r.duration_s < 60 else f"{r.duration_s / 60:.1f}m"

        table.add_row(
            str(r.trade_id),
            f"[{dir_color}]{r.direction}[/{dir_color}]",
            entry_ts,
            f"{r.entry_price:>11,.2f}",
            f"{r.exit_price:>11,.2f}",
            f"[{pnl_color}]{gpnl_sign}{r.gross_pnl:.4f}[/{pnl_color}]",
            f"{r.total_fees:.4f}",
            f"[{pnl_color}]{npnl_sign}{r.net_pnl:.4f}[/{pnl_color}]",
            f"[{res_color}]{r.outcome}[/{res_color}]",
            duration_str,
            f"{r.balance_after:>11,.4f}",
        )

    _CONSOLE.print(table)


def _render_performance_panel(report: PerformanceReport) -> None:
    """Print a two-column Rich Table with the aggregate performance metrics."""
    pf_str: str = f"{report.profit_factor:.3f}" if report.profit_factor != float("inf") else "INF"
    net_color: str = "bold green" if report.net_pnl >= 0 else "bold red"
    pf_color: str = "bold green" if report.profit_factor >= 1 else "bold red"
    wr_color: str = "bold green" if report.win_rate_pct >= 50 else "bold red"
    sharpe_str: str = f"{report.sharpe_ratio:.3f}" if report.sharpe_ratio != float("inf") else "INF"
    sortino_str: str = f"{report.sortino_ratio:.3f}" if report.sortino_ratio != float("inf") else "INF"
    calmar_str: str = f"{report.calmar_ratio:.3f}" if report.calmar_ratio != float("inf") else "INF"
    wl_str: str = f"{report.win_loss_ratio:.3f}" if report.win_loss_ratio != float("inf") else "INF"
    sharpe_color: str = "bold green" if report.sharpe_ratio > 0 else "bold red"
    sortino_color: str = "bold green" if report.sortino_ratio > 0 else "bold red"
    calmar_color: str = "bold green" if report.calmar_ratio > 0 else "bold red"
    wl_color: str = "bold green" if report.win_loss_ratio >= 1 else "bold red"

    table = Table(
        box=box.SIMPLE_HEAVY,
        show_header=False,
        border_style="bright_blue",
        padding=(0, 2),
        expand=False,
    )
    table.add_column("Metric", style="dim white", min_width=26)
    table.add_column("Value", min_width=18)

    rows: list[tuple[str, str]] = [
        ("Initial Balance", f"[white]${report.initial_balance:,.2f}[/white]"),
        ("Final Balance", f"[white]${report.final_balance:,.4f}[/white]"),
        ("Net PnL", f"[{net_color}]${report.net_pnl:+,.4f}[/{net_color}]"),
        ("Return", f"[{net_color}]{report.return_pct:+.4f}%[/{net_color}]"),
        ("", ""),
        ("Total Trades", f"[white]{report.total_trades}[/white]"),
        ("Winning Trades", f"[bold green]{report.winning_trades}[/bold green]"),
        ("Losing Trades", f"[bold red]{report.losing_trades}[/bold red]"),
        ("Win Rate", f"[{wr_color}]{report.win_rate_pct:.1f}%[/{wr_color}]"),
        ("", ""),
        ("Gross Profit", f"[green]${report.gross_profit:,.4f}[/green]"),
        ("Gross Loss", f"[red]${report.gross_loss:,.4f}[/red]"),
        ("Profit Factor", f"[{pf_color}]{pf_str}[/{pf_color}]"),
        ("Win / Loss Ratio", f"[{wl_color}]{wl_str}[/{wl_color}]"),
        ("", ""),
        ("Sharpe Ratio (per-trade)", f"[{sharpe_color}]{sharpe_str}[/{sharpe_color}]"),
        ("Sortino Ratio", f"[{sortino_color}]{sortino_str}[/{sortino_color}]"),
        ("Calmar Ratio", f"[{calmar_color}]{calmar_str}[/{calmar_color}]"),
        ("", ""),
        ("Avg Win", f"[green]${report.avg_win_usdt:,.4f}[/green]"),
        ("Avg Loss", f"[red]${report.avg_loss_usdt:,.4f}[/red]"),
        ("Avg Trade Duration", f"[white]{report.avg_duration_s:.1f}s[/white]"),
        ("", ""),
        ("Max Drawdown (USDT)", f"[red]${report.max_drawdown_usdt:,.4f}[/red]"),
        ("Max Drawdown (%)", f"[red]{report.max_drawdown_pct:.4f}%[/red]"),
    ]

    for metric, value in rows:
        table.add_row(metric, value)

    label: str = sample_size_label(report.total_trades)
    label_color: str = "bold green" if report.total_trades >= _SAMPLE_OK_TRADES else "bold yellow"
    if report.total_trades < MIN_TRADES_FOR_SIGNIFICANCE:
        label_color = "bold red"
    table.add_row("", "")
    table.add_row(
        f"Sample size (min {MIN_TRADES_FOR_SIGNIFICANCE})",
        f"[{label_color}]{label} — {report.total_trades} trades[/{label_color}]",
    )

    _CONSOLE.print(
        Panel(
            table,
            title="[bold bright_white]Performance Report[/bold bright_white]",
            border_style="bright_blue",
            padding=(0, 1),
        )
    )


def _parse_args() -> argparse.Namespace:
    """Parse CLI overrides for data directory, detector, and trade parameters."""
    parser = argparse.ArgumentParser(description="SMC event-driven backtester for V3-Crypto-Quant")
    parser.add_argument("--data-dir", type=pathlib.Path, default=pathlib.Path("data"))
    parser.add_argument("--z-threshold", type=float, default=2.0)
    parser.add_argument("--div-window", type=int, default=100)
    parser.add_argument("--symbol", type=str, default=None, help="Symbol filter, e.g. btc -> bbo_btc_* / trades_btc_*")
    parser.add_argument("--balance", type=float, default=10_000.0)
    parser.add_argument(
        "--position-usdt",
        type=float,
        default=None,
        help="Fixed notional per trade in USDT; omit to size with PositionSizer (Kelly x GEX)",
    )
    parser.add_argument(
        "--tp-pct", type=float, default=SETTINGS.strategy.tp_pct, help="Take Profit as fraction of entry"
    )
    parser.add_argument("--sl-pct", type=float, default=SETTINGS.strategy.sl_pct, help="Stop Loss as fraction of entry")
    parser.add_argument("--tp", type=float, default=None, help="Fixed Take Profit in USDT (overrides --tp-pct)")
    parser.add_argument("--sl", type=float, default=None, help="Fixed Stop Loss in USDT (overrides --sl-pct)")
    parser.add_argument(
        "--fee-rate",
        type=float,
        default=SETTINGS.strategy.taker_fee_rate,
        help="Taker fee rate, e.g. 0.00045 = 0.045%%",
    )
    parser.add_argument(
        "--slippage-bps", type=float, default=SETTINGS.strategy.slippage_bps, help="Adverse slippage per fill in bps"
    )
    parser.add_argument("--cooldown-s", type=int, default=60, help="Post-trade cooldown in seconds")
    parser.add_argument(
        "--trailing-activation", type=float, default=100.0, help="Profit distance in USDT to move SL to break-even"
    )
    parser.add_argument(
        "--max-duration-s", type=int, default=1800, help="Maximum trade duration in seconds before time-based exit"
    )
    parser.add_argument(
        "--gex-history-dir",
        type=pathlib.Path,
        default=_DEFAULT_GEX_HISTORY_DIR,
        help="Directory of gex_YYYYMMDD.jsonl snapshots; GEX is looked up at each signal's timestamp",
    )
    parser.add_argument(
        "--gex-file",
        type=pathlib.Path,
        default=None,
        help="Static GEX snapshot applied to ALL ticks (look-ahead bias; use only for smoke tests)",
    )
    return parser.parse_args()


def main() -> None:
    """Orchestrate data loading, signal generation, backtest loop, and reporting."""
    args = _parse_args()

    use_usdt_levels: bool = args.tp is not None or args.sl is not None
    config = TradeConfig(
        initial_balance=args.balance,
        position_size_usdt=args.position_usdt if args.position_usdt is not None else 1_000.0,
        take_profit_usdt=args.tp if args.tp is not None else 300.0,
        stop_loss_usdt=args.sl if args.sl is not None else 150.0,
        tp_pct=None if use_usdt_levels else args.tp_pct,
        sl_pct=None if use_usdt_levels else args.sl_pct,
        fee_rate=args.fee_rate,
        slippage_bps=args.slippage_bps,
        cooldown_ms=args.cooldown_s * 1_000,
        trailing_activation_usdt=args.trailing_activation,
        max_duration_s=args.max_duration_s,
        max_order_size_btc=SETTINGS.max_order_size_btc,
        max_leverage=float(SETTINGS.default_leverage),
    )

    gex_manager: GexRiskManager | None = None
    gex_history: GexHistory | None = None
    if args.gex_file is not None:
        if not args.gex_file.exists():
            _CONSOLE.print(f"[red]GEX snapshot not found: {args.gex_file}[/red]")
            return
        gex_map: dict[float, float] = load_gex_map_from_file(args.gex_file)
        if not gex_map:
            _CONSOLE.print(f"[red]GEX snapshot is empty: {args.gex_file}[/red]")
            return
        gex_manager = GexRiskManager(gex_map=gex_map)
        _CONSOLE.print(
            "[bold yellow]WARNING: one static GEX snapshot is applied to the whole history (look-ahead bias). "
            "Results are not valid for performance claims.[/bold yellow]"
        )
    else:
        if not args.gex_history_dir.is_dir():
            _CONSOLE.print(
                f"[red]No GEX history in {args.gex_history_dir} — run data_ingestion/deribit_client.py "
                "to record it, or pass --gex-file for a smoke test.[/red]"
            )
            return
        gex_history = GexHistory.from_dir(args.gex_history_dir)
        if len(gex_history) == 0:
            _CONSOLE.print(f"[red]GEX history is empty: {args.gex_history_dir}[/red]")
            return

    confluence = SuperConfluenceFilter()

    _CONSOLE.print("[dim]Loading tick data ...[/dim]")
    try:
        df: pd.DataFrame = load_merged_frame(args.data_dir, symbol=args.symbol)
    except ValueError as exc:
        _CONSOLE.print(f"[red]{exc}[/red]")
        return

    detector = MicrostructureDetector(
        divergence_window=args.div_window,
        cvd_z_threshold=args.z_threshold,
    )
    _CONSOLE.print("[dim]Running detector ...[/dim]")
    df = detector.smooth_ofi(df)
    signals: pd.DataFrame = detector.detect_cvd_divergence(df)

    if signals.empty:
        _CONSOLE.print("[yellow]No signals detected — backtest aborted.[/yellow]")
        return

    signal_map: dict[int, str] = dict(zip(signals["timestamp_ms"].to_numpy(), signals["signal"].to_numpy()))
    gex_source: str = (
        f"static file {args.gex_file}"
        if gex_history is None
        else f"{len(gex_history)} snapshots in {args.gex_history_dir}"
    )
    _CONSOLE.print(
        f"[dim]Running event loop over {len(df):,} ticks with {len(signal_map)} signals (GEX: {gex_source}) ...[/dim]\n"
    )

    engine = BacktestEngine(
        config=config,
        signal_map=signal_map,
        gex_manager=gex_manager,
        confluence=confluence,
        position_sizer=PositionSizer() if args.position_usdt is None else None,
        gex_history=gex_history,
    )
    for row in df.itertuples(index=False):
        engine.process_tick(row)

    if engine.has_open_position:
        _CONSOLE.print("[yellow]Note: 1 position was still open at end of data and was not counted.[/yellow]\n")

    records: list[TradeRecord] = engine.trade_records
    report: PerformanceReport = compute_performance_report(records, engine.equity_curve, config)

    if not records:
        _CONSOLE.print("[yellow]No trades were executed.[/yellow]")
        return

    _render_trade_log(records, config)
    _CONSOLE.print()
    _render_performance_panel(report)
    _CONSOLE.print()


if __name__ == "__main__":
    main()
