"""Standalone SMC signal scanner for offline microstructure analysis.

Loads all Parquet tick data from the data/ directory, reconstructs the same
merged BBO + Trade frame used by the interactive visualiser, runs the
MicrostructureDetector to identify CVD / Price divergences, and renders a
formatted Rich terminal report with session-level statistics and a table of
every detected absorption event.

Usage (from project root)
--------------------------
    python research/signal_scanner.py
    python research/signal_scanner.py --data-dir path/to/data --z-threshold 2.5
"""

from __future__ import annotations

import argparse
import pathlib
import sys
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from quant_models.smc_detectors import MicrostructureDetector

_CONSOLE: Final[Console] = Console()


def _load_parquet_glob(data_dir: pathlib.Path, prefix: str) -> pd.DataFrame:
    """Concatenate every Parquet file whose name starts with *prefix*.

    Files are sorted by filename (which encodes a UTC timestamp suffix) so
    they are concatenated in chronological order.  The result is re-sorted
    by ``timestamp_ms`` and returned with a clean integer index.

    Raises ``FileNotFoundError`` when no matching files exist in *data_dir*.
    """
    files: list[pathlib.Path] = sorted(data_dir.glob(f"{prefix}_*.parquet"))
    if not files:
        raise FileNotFoundError(
            f"No Parquet files with prefix '{prefix}' found in '{data_dir}'"
        )
    return (
        pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        .sort_values("timestamp_ms", kind="stable")
        .reset_index(drop=True)
    )


def load_merged_frame(data_dir: pathlib.Path) -> pd.DataFrame:
    """Build a merged BBO + Trade DataFrame aligned by timestamp.

    Pipeline:

    1. Load all trade Parquet files; compute the running signed CVD as
       ``cumsum`` of ``+qty`` (taker buy) / ``-qty`` (taker sell).
    2. Load all BBO Parquet files; filter warmup rows where
       ``best_ask_price == 0`` (arrives before the local book is populated).
    3. Align via ``pd.merge_asof`` (BBO left, trades right, backward fill)
       so every 100 ms BBO tick is enriched with the most recent trade
       price and running CVD.

    Returns a DataFrame at 100 ms cadence containing the columns:
    ``timestamp_ms``, ``dt``, ``best_bid_price``, ``best_ask_price``,
    ``best_bid_qty``, ``best_ask_qty``, ``spread``, ``ofi``, ``price``,
    ``cvd``.
    """
    trades: pd.DataFrame = _load_parquet_glob(data_dir, "trades")
    trades["signed_qty"] = trades["qty"].where(~trades["is_buyer_maker"], -trades["qty"])
    trades["cvd"] = trades["signed_qty"].cumsum()
    trades["dt"] = pd.to_datetime(trades["timestamp_ms"], unit="ms", utc=True)

    bbo: pd.DataFrame = _load_parquet_glob(data_dir, "bbo")
    bbo = bbo[bbo["best_ask_price"] > 0].reset_index(drop=True)
    bbo["dt"] = pd.to_datetime(bbo["timestamp_ms"], unit="ms", utc=True)

    return pd.merge_asof(
        bbo,
        trades[["timestamp_ms", "price", "cvd"]],
        on="timestamp_ms",
        direction="backward",
    )


def _render_summary(
    df: pd.DataFrame,
    signals: pd.DataFrame,
    detector: MicrostructureDetector,
) -> None:
    """Print a Rich Panel with session-level statistics and detector config."""
    n_bull: int = int((signals["signal"] == "BULLISH_ABSORPTION").sum())
    n_bear: int = int((signals["signal"] == "BEARISH_ABSORPTION").sum())
    cvd_final: float = float(df["cvd"].iloc[-1])
    cvd_color: str = "bold green" if cvd_final >= 0 else "bold red"
    cvd_sign: str = "+" if cvd_final >= 0 else ""

    period_start: str = df["dt"].min().strftime("%H:%M:%S")
    period_end: str = df["dt"].max().strftime("%H:%M:%S")
    duration_min: float = (df["dt"].max() - df["dt"].min()).total_seconds() / 60

    stats_text = Text.assemble(
        ("Period   ", "dim"), (f"{period_start} - {period_end} UTC", "cyan"),
        ("   Ticks  ", "dim"), (f"{len(df):,}", "white"),
        ("   Duration  ", "dim"), (f"{duration_min:.1f} min\n", "white"),
        ("CVD Final  ", "dim"), (f"{cvd_sign}{cvd_final:.4f} BTC", cvd_color),
        ("   OFI Median  ", "dim"),
        (f"{df['ofi_smooth'].median():.6f}", "white"),
        ("   Signals  ", "dim"),
        (f"{n_bull} Bullish  ", "bold green"),
        (f"{n_bear} Bearish", "bold red"),
    )
    _CONSOLE.print(
        Panel(
            stats_text,
            title="[bold bright_white]V3-Crypto-Quant  |  SMC Signal Scanner[/bold bright_white]",
            border_style="bright_blue",
            padding=(0, 2),
        )
    )


def _render_signals_table(signals: pd.DataFrame) -> None:
    """Print a Rich Table listing all detected divergence and absorption events.

    Each row is coloured green for bullish absorption and red for bearish
    absorption.  The table is iterated with ``itertuples`` (faster than
    ``iterrows`` as it avoids per-row Series construction overhead).
    """
    table = Table(
        title=f"[bold]Detected SMC Signals   ({len(signals)} events after deduplication)[/bold]",
        box=box.ROUNDED,
        header_style="bold white",
        border_style="bright_blue",
        show_lines=False,
        expand=False,
    )
    table.add_column("#",             style="dim",         justify="right",  min_width=3)
    table.add_column("Timestamp (UTC)",                    justify="left",   min_width=26)
    table.add_column("Signal",                             justify="left",   min_width=22)
    table.add_column("Price (USDT)",  style="white",       justify="right",  min_width=14)
    table.add_column("dPrice",                             justify="right",  min_width=10)
    table.add_column("dCVD (BTC)",                         justify="right",  min_width=13)
    table.add_column("OFI Smooth",    style="dim white",   justify="right",  min_width=12)

    for i, row in enumerate(signals.itertuples(index=False), start=1):
        is_bull: bool = row.signal == "BULLISH_ABSORPTION"
        color: str = "bold green" if is_bull else "bold red"
        dp_sign: str = "+" if row.price_delta >= 0 else ""
        dc_sign: str = "+" if row.cvd_delta >= 0 else ""
        ts_str: str = str(row.dt).replace("+00:00", "").strip()

        table.add_row(
            str(i),
            f"[cyan]{ts_str}[/cyan]",
            f"[{color}]{row.signal}[/{color}]",
            f"{row.price:>13,.2f}",
            f"[{color}]{dp_sign}{row.price_delta:.3f}[/{color}]",
            f"[{color}]{dc_sign}{row.cvd_delta:.4f}[/{color}]",
            f"{row.ofi_smooth:.6f}",
        )

    _CONSOLE.print(table)


def _render_empty_result(z_threshold: float, price_tolerance: float) -> None:
    """Print a diagnostic message when no signals pass the current thresholds."""
    _CONSOLE.print(
        Panel(
            f"[yellow]No divergence signals detected.[/yellow]\n"
            f"Current thresholds: cvd_z={z_threshold}, price_tol={price_tolerance}\n"
            f"Try lowering [bold]--z-threshold[/bold] or raising "
            f"[bold]--price-tolerance[/bold].",
            border_style="yellow",
            title="[yellow]No Signals[/yellow]",
        )
    )


def _parse_args() -> argparse.Namespace:
    """Parse optional CLI overrides for data directory and detector parameters."""
    parser = argparse.ArgumentParser(
        description="SMC microstructure signal scanner for V3-Crypto-Quant"
    )
    parser.add_argument(
        "--data-dir",
        type=pathlib.Path,
        default=pathlib.Path("data"),
        help="Directory containing trade_*.parquet and bbo_*.parquet files",
    )
    parser.add_argument(
        "--ofi-window",
        type=int,
        default=50,
        help="Rolling window (ticks) for OFI smoothing  [default: 50]",
    )
    parser.add_argument(
        "--div-window",
        type=int,
        default=100,
        help="Look-back window (ticks) for divergence delta  [default: 100]",
    )
    parser.add_argument(
        "--z-threshold",
        type=float,
        default=2.0,
        help="CVD delta z-score threshold for signal detection  [default: 2.0]",
    )
    parser.add_argument(
        "--price-tolerance",
        type=float,
        default=0.5,
        help="Max price delta z-score for a divergence to be counted  [default: 0.5]",
    )
    return parser.parse_args()


def main() -> None:
    """Orchestrate data loading, signal detection, and terminal report rendering."""
    args = _parse_args()

    _CONSOLE.print("[dim]Loading tick data ...[/dim]")
    df: pd.DataFrame = load_merged_frame(args.data_dir)
    _CONSOLE.print(f"[dim]Loaded {len(df):,} merged ticks.[/dim]")

    detector = MicrostructureDetector(
        ofi_smooth_window=args.ofi_window,
        divergence_window=args.div_window,
        cvd_z_threshold=args.z_threshold,
        price_tolerance=args.price_tolerance,
    )

    _CONSOLE.print("[dim]Smoothing OFI ...[/dim]")
    df = detector.smooth_ofi(df)

    _CONSOLE.print("[dim]Scanning for CVD divergences ...[/dim]\n")
    signals: pd.DataFrame = detector.detect_cvd_divergence(df)

    _render_summary(df, signals, detector)
    _CONSOLE.print()

    if signals.empty:
        _render_empty_result(args.z_threshold, args.price_tolerance)
    else:
        _render_signals_table(signals)

    _CONSOLE.print()


if __name__ == "__main__":
    main()
