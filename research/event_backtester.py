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
    python research/event_backtester.py
    python research/event_backtester.py --tp 100 --sl 50 --position-usdt 2000
    python research/event_backtester.py --gex-file data/live_gex.json
"""

from __future__ import annotations

import argparse
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

from quant_models.gex_risk_manager import GexRiskManager, load_gex_map_from_file
from quant_models.smc_detectors import MicrostructureDetector
from quant_models.super_confluence import SuperConfluenceFilter
from research.data_loader import load_merged_frame

_CONSOLE: Final[Console] = Console()
_DATA_DIR: Final[pathlib.Path] = pathlib.Path("data")
_DEFAULT_GEX_FILE: Final[pathlib.Path] = _DATA_DIR / "live_gex.json"


@dataclass(frozen=True)
class TradeConfig:
    """Immutable configuration for the backtesting engine."""

    initial_balance: float = 10_000.0
    position_size_usdt: float = 1_000.0
    take_profit_usdt: float = 300.0
    stop_loss_usdt: float = 150.0
    fee_rate: float = 0.0004
    cooldown_ms: int = 60_000
    trailing_activation_usdt: float = 100.0
    max_duration_s: int = 1800


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
        gex_manager: GexRiskManager,
        confluence: SuperConfluenceFilter | None = None,
    ) -> None:
        self._config: TradeConfig = config
        self._signal_map: dict[int, str] = signal_map
        self._gex: GexRiskManager = gex_manager
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
            verdict = self._confluence.evaluate(
                signal_type=signal,
                price=float(row.price),
                vwap=vwap,
                gex_map=self._gex.get_gex_map(),
            )
            if verdict.allowed:
                self._open_position(signal, row.price, row.dt)

    def _check_exit(self, price: float, dt: pd.Timestamp) -> tuple[str, float] | None:
        """Return ``(outcome, fill_price)`` if any exit condition is met.

        Evaluation order: time-based exit → trailing stop update → TP → SL.
        TP fill price is clamped to the exact level; TIME_EXIT and trailing-SL
        fills use the live tick price / current SL price respectively.
        """
        elapsed_s: float = (dt - self._entry_time).total_seconds()  # type: ignore[operator]
        if elapsed_s >= self._config.max_duration_s:
            return ("TIME_EXIT", price)

        tp_usdt: float = self._config.take_profit_usdt
        activation: float = self._config.trailing_activation_usdt

        if self._position == "LONG":
            if price >= self._entry_price + activation:
                self._current_sl_price = max(self._current_sl_price, self._entry_price)
            if price >= self._entry_price + tp_usdt:
                return ("TP", self._entry_price + tp_usdt)
            if price <= self._current_sl_price:
                return ("SL", self._current_sl_price)
        elif self._position == "SHORT":
            if price <= self._entry_price - activation:
                self._current_sl_price = min(self._current_sl_price, self._entry_price)
            if price <= self._entry_price - tp_usdt:
                return ("TP", self._entry_price - tp_usdt)
            if price >= self._current_sl_price:
                return ("SL", self._current_sl_price)
        return None

    def _open_position(self, signal: str, price: float, dt: pd.Timestamp) -> None:
        """Record entry metadata and deduct the entry taker fee from balance."""
        self._position = "LONG" if signal == "BULLISH_ABSORPTION" else "SHORT"
        self._entry_price = price
        self._entry_time = dt
        self._position_btc = self._config.position_size_usdt / price
        self._entry_fee = self._config.fee_rate * self._config.position_size_usdt
        self._balance -= self._entry_fee
        if self._position == "LONG":
            self._current_sl_price = price - self._config.stop_loss_usdt
        else:
            self._current_sl_price = price + self._config.stop_loss_usdt

    def _close_position(
        self,
        outcome: str,
        exit_price: float,
        dt: pd.Timestamp,
        ts_ms: int,
    ) -> None:
        """Settle the trade, record it, update balance, and start cooldown."""
        exit_notional: float = self._position_btc * exit_price
        exit_fee: float = self._config.fee_rate * exit_notional
        total_fees: float = self._entry_fee + exit_fee

        gross_pnl: float = (
            self._position_btc * (exit_price - self._entry_price)
            if self._position == "LONG"
            else self._position_btc * (self._entry_price - exit_price)
        )
        net_pnl: float = gross_pnl - exit_fee
        self._balance += net_pnl

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

    wins: list[TradeRecord] = [r for r in records if r.outcome == "TP" or (r.outcome == "TIME_EXIT" and r.net_pnl > 0)]
    losses: list[TradeRecord] = [
        r for r in records if r.outcome == "SL" or (r.outcome == "TIME_EXIT" and r.net_pnl <= 0)
    ]

    gross_profit: float = sum(r.net_pnl for r in wins)
    gross_loss: float = abs(sum(r.net_pnl for r in losses))
    profit_factor: float = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    equity: pd.Series = pd.Series(equity_curve, dtype=float)
    rolling_peak: pd.Series = equity.cummax()
    drawdown_usdt: pd.Series = rolling_peak - equity
    drawdown_pct: pd.Series = (drawdown_usdt / rolling_peak) * 100

    net_pnl: float = sum(r.net_pnl for r in records)
    final_balance: float = equity_curve[-1]

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
        return_pct=net_pnl / config.initial_balance * 100,
        max_drawdown_usdt=float(drawdown_usdt.max()),
        max_drawdown_pct=float(drawdown_pct.max()),
        avg_win_usdt=gross_profit / len(wins) if wins else 0.0,
        avg_loss_usdt=-(gross_loss / len(losses)) if losses else 0.0,
        avg_duration_s=sum(r.duration_s for r in records) / len(records),
    )


def _render_trade_log(records: list[TradeRecord], config: TradeConfig) -> None:
    """Print a Rich Table with one row per completed trade."""
    table = Table(
        title=f"Trade Log  ({len(records)} trades  |  "
        f"position={config.position_size_usdt:.0f} USDT  "
        f"TP={config.take_profit_usdt:.0f}  SL={config.stop_loss_usdt:.0f})",
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
        is_win: bool = r.outcome == "TP" or (r.outcome == "TIME_EXIT" and r.net_pnl > 0)
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
    parser.add_argument("--balance", type=float, default=10_000.0)
    parser.add_argument("--position-usdt", type=float, default=1_000.0, help="Fixed notional per trade in USDT")
    parser.add_argument("--tp", type=float, default=300.0, help="Take Profit distance from entry in USDT")
    parser.add_argument("--sl", type=float, default=150.0, help="Stop Loss distance from entry in USDT")
    parser.add_argument("--fee-rate", type=float, default=0.0004, help="Taker fee rate, e.g. 0.0004 = 0.04%%")
    parser.add_argument("--cooldown-s", type=int, default=60, help="Post-trade cooldown in seconds")
    parser.add_argument(
        "--trailing-activation", type=float, default=100.0, help="Profit distance in USDT to move SL to break-even"
    )
    parser.add_argument(
        "--max-duration-s", type=int, default=1800, help="Maximum trade duration in seconds before time-based exit"
    )
    parser.add_argument(
        "--gex-file",
        type=pathlib.Path,
        default=_DEFAULT_GEX_FILE,
        help="Deribit-style GEX JSON snapshot used by SuperConfluenceFilter",
    )
    return parser.parse_args()


def main() -> None:
    """Orchestrate data loading, signal generation, backtest loop, and reporting."""
    args = _parse_args()

    config = TradeConfig(
        initial_balance=args.balance,
        position_size_usdt=args.position_usdt,
        take_profit_usdt=args.tp,
        stop_loss_usdt=args.sl,
        fee_rate=args.fee_rate,
        cooldown_ms=args.cooldown_s * 1_000,
        trailing_activation_usdt=args.trailing_activation,
        max_duration_s=args.max_duration_s,
    )

    if not args.gex_file.exists():
        _CONSOLE.print(
            f"[red]GEX snapshot not found: {args.gex_file} — run the Deribit client or pass --gex-file.[/red]"
        )
        return

    gex_map: dict[float, float] = load_gex_map_from_file(args.gex_file)
    if not gex_map:
        _CONSOLE.print(f"[red]GEX snapshot is empty: {args.gex_file}[/red]")
        return

    gex_manager = GexRiskManager(gex_map=gex_map)
    confluence = SuperConfluenceFilter()

    _CONSOLE.print("[dim]Loading tick data ...[/dim]")
    df: pd.DataFrame = load_merged_frame(args.data_dir)

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
    _CONSOLE.print(
        f"[dim]Running event loop over {len(df):,} ticks with "
        f"{len(signal_map)} signals "
        f"(GEX strikes={len(gex_map)}, file={args.gex_file}) ...[/dim]\n"
    )

    engine = BacktestEngine(
        config=config,
        signal_map=signal_map,
        gex_manager=gex_manager,
        confluence=confluence,
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
