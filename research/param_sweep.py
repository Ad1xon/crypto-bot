"""Parameter sweep and Walk-Forward validation for SMC absorption signals.

Performs grid search across detector hyper-parameters (divergence window, CVD
z-threshold) and execution risk parameters (TP, SL, trailing stop), splitting
the historical tick dataset into chronologically disjoint In-Sample (IS, default 70%)
and Out-of-Sample (OOS, default 30%) partitions to identify robust, non-overfit
trading configurations.

Walk-Forward & Anti-Overfitting Philosophy
------------------------------------------
- Chronological train/test split prevents look-ahead and temporal leakage.
- Independent signal detection on each partition avoids cross-boundary filtering bias.
- Evaluation compares IS performance vs OOS decay (e.g. OOS Profit Factor and Sharpe).
- Parameters that perform well IS but collapse OOS are flagged as OVERFIT.

Usage (from project root)
-------------------------
    python research/param_sweep.py
    python research/param_sweep.py --split-ratio 0.70 --data-dir data/
    python research/param_sweep.py --export-json results/sweep_results.json
"""

from __future__ import annotations

import argparse
import itertools
import json
import pathlib
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from rich import box
from rich.console import Console
from rich.table import Table

from quant_models.gex_risk_manager import GexRiskManager, load_gex_map_from_file
from quant_models.smc_detectors import MicrostructureDetector
from quant_models.super_confluence import SuperConfluenceFilter
from research.data_loader import load_merged_frame
from research.event_backtester import (
    BacktestEngine,
    PerformanceReport,
    TradeConfig,
    compute_performance_report,
)

_CONSOLE: Final[Console] = Console()
_DEFAULT_DATA_DIR: Final[pathlib.Path] = pathlib.Path("data")
_DEFAULT_GEX_FILE: Final[pathlib.Path] = _DEFAULT_DATA_DIR / "live_gex.json"


@dataclass(frozen=True)
class SweepParams:
    """Hyper-parameters evaluated during a grid search iteration."""

    z_threshold: float = 2.0
    div_window: int = 100
    tp_pct: float | None = None
    sl_pct: float | None = None
    tp_usdt: float | None = 300.0
    sl_usdt: float | None = 150.0
    trailing_activation_pct: float | None = None
    trailing_activation_usdt: float = 100.0


@dataclass(frozen=True)
class SweepResult:
    """Consolidated Walk-Forward validation result for a parameter set."""

    params: SweepParams
    is_report: PerformanceReport
    oos_report: PerformanceReport
    robustness_score: float
    status: str  # ROBUST, DEGRADED, OVERFIT, INSUFFICIENT_TRADES


def run_backtest_slice(
    df: pd.DataFrame,
    params: SweepParams,
    gex_manager: GexRiskManager,
    base_config: TradeConfig,
    confluence: SuperConfluenceFilter | None = None,
) -> PerformanceReport:
    """Run detection and event-driven backtesting on a DataFrame slice.

    Parameters are mapped into a specific :class:`TradeConfig`.
    If no signals are detected, returns an empty :class:`PerformanceReport`.
    """
    if df.empty or len(df) < params.div_window:
        return compute_performance_report([], [base_config.initial_balance], base_config)

    config = TradeConfig(
        initial_balance=base_config.initial_balance,
        position_size_usdt=base_config.position_size_usdt,
        take_profit_usdt=params.tp_usdt if params.tp_usdt is not None else base_config.take_profit_usdt,
        stop_loss_usdt=params.sl_usdt if params.sl_usdt is not None else base_config.stop_loss_usdt,
        tp_pct=params.tp_pct,
        sl_pct=params.sl_pct,
        trailing_activation_pct=params.trailing_activation_pct,
        trailing_activation_usdt=params.trailing_activation_usdt,
        fee_rate=base_config.fee_rate,
        cooldown_ms=base_config.cooldown_ms,
        max_duration_s=base_config.max_duration_s,
    )

    detector = MicrostructureDetector(
        divergence_window=params.div_window,
        cvd_z_threshold=params.z_threshold,
    )

    df_prepared = detector.smooth_ofi(df)
    signals = detector.detect_cvd_divergence(df_prepared)

    if signals.empty:
        return compute_performance_report([], [config.initial_balance], config)

    signal_map: dict[int, str] = dict(zip(signals["timestamp_ms"].to_numpy(), signals["signal"].to_numpy()))

    engine = BacktestEngine(
        config=config,
        signal_map=signal_map,
        gex_manager=gex_manager,
        confluence=confluence,
    )

    for row in df_prepared.itertuples(index=False):
        engine.process_tick(row)

    return compute_performance_report(engine.trade_records, engine.equity_curve, config)


def evaluate_robustness(is_rep: PerformanceReport, oos_rep: PerformanceReport) -> tuple[float, str]:
    """Classify Walk-Forward robustness based on IS vs OOS metric persistence.

    Returns:
        (robustness_score, status_label)
    """
    if is_rep.total_trades < 2 or oos_rep.total_trades < 2:
        return (0.0, "INSUFFICIENT_TRADES")

    if oos_rep.profit_factor < 1.0 or oos_rep.net_pnl < 0:
        return (0.0, "OVERFIT")

    # Ratio of OOS Profit Factor to IS Profit Factor capped at 2.0
    pf_ratio = min(oos_rep.profit_factor / max(is_rep.profit_factor, 0.01), 2.0)
    score = round(pf_ratio, 3)

    if pf_ratio >= 0.70 and oos_rep.sharpe_ratio > 0.0:
        return (score, "ROBUST")
    else:
        return (score, "DEGRADED")


def run_param_sweep(
    df: pd.DataFrame,
    param_grid: list[SweepParams],
    gex_manager: GexRiskManager,
    split_ratio: float = 0.70,
    base_config: TradeConfig | None = None,
    confluence: SuperConfluenceFilter | None = None,
) -> list[SweepResult]:
    """Execute grid search across parameter combinations with chronological IS/OOS split."""
    if base_config is None:
        base_config = TradeConfig()

    split_idx = int(len(df) * split_ratio)
    df_train = df.iloc[:split_idx].copy().reset_index(drop=True)
    df_test = df.iloc[split_idx:].copy().reset_index(drop=True)

    results: list[SweepResult] = []

    for params in param_grid:
        is_report = run_backtest_slice(df_train, params, gex_manager, base_config, confluence)
        oos_report = run_backtest_slice(df_test, params, gex_manager, base_config, confluence)
        score, status = evaluate_robustness(is_report, oos_report)

        results.append(
            SweepResult(
                params=params,
                is_report=is_report,
                oos_report=oos_report,
                robustness_score=score,
                status=status,
            )
        )

    # Sort primarily by status priority (ROBUST > DEGRADED > OVERFIT > INSUFFICIENT),
    # then by OOS Sharpe, then IS Sharpe
    status_priority = {"ROBUST": 3, "DEGRADED": 2, "OVERFIT": 1, "INSUFFICIENT_TRADES": 0}
    results.sort(
        key=lambda r: (
            status_priority.get(r.status, 0),
            r.oos_report.sharpe_ratio,
            r.is_report.sharpe_ratio,
        ),
        reverse=True,
    )
    return results


def default_param_grid() -> list[SweepParams]:
    """Build a standard grid of detector and risk parameters."""
    z_thresholds = [1.5, 2.0, 2.5]
    div_windows = [50, 100, 150]
    pct_pairs = [
        (0.003, 0.0015),  # 0.3% TP, 0.15% SL
        (0.006, 0.0030),  # 0.6% TP, 0.30% SL
        (0.010, 0.0050),  # 1.0% TP, 0.50% SL
    ]

    grid: list[SweepParams] = []
    for z, win, (tp_pct, sl_pct) in itertools.product(z_thresholds, div_windows, pct_pairs):
        grid.append(
            SweepParams(
                z_threshold=z,
                div_window=win,
                tp_pct=tp_pct,
                sl_pct=sl_pct,
                tp_usdt=None,
                sl_usdt=None,
                trailing_activation_pct=tp_pct / 3.0,
            )
        )
    return grid


def render_sweep_table(results: list[SweepResult], top_n: int = 15, console: Console | None = None) -> None:
    """Render a Rich Table summarizing top parameter sets and Walk-Forward robustness."""
    c = console if console is not None else _CONSOLE

    table = Table(
        title=f"Walk-Forward Parameter Sweep (Top {min(top_n, len(results))} of {len(results)} Combinations)",
        box=box.ROUNDED,
        header_style="bold bright_white",
        border_style="bright_blue",
    )

    table.add_column("#", justify="right", style="dim", min_width=3)
    table.add_column("Z", justify="right", min_width=4)
    table.add_column("Win", justify="right", min_width=4)
    table.add_column("TP / SL", justify="center", min_width=11)
    table.add_column("IS Trd", justify="right", min_width=6)
    table.add_column("IS Win%", justify="right", min_width=7)
    table.add_column("IS PF", justify="right", min_width=6)
    table.add_column("IS Sharpe", justify="right", min_width=9)
    table.add_column("OOS Trd", justify="right", min_width=7)
    table.add_column("OOS Win%", justify="right", min_width=8)
    table.add_column("OOS PF", justify="right", min_width=6)
    table.add_column("OOS Sharpe", justify="right", min_width=10)
    table.add_column("Status", justify="center", min_width=12)

    status_colors = {
        "ROBUST": "bold green",
        "DEGRADED": "bold yellow",
        "OVERFIT": "bold red",
        "INSUFFICIENT_TRADES": "dim white",
    }

    for idx, r in enumerate(results[:top_n], start=1):
        p = r.params
        tp_sl_str = (
            f"{p.tp_pct * 100:.2f}%/{p.sl_pct * 100:.2f}%"
            if p.tp_pct is not None and p.sl_pct is not None
            else f"${p.tp_usdt:.0f}/${p.sl_usdt:.0f}"
        )

        is_pf_str = f"{r.is_report.profit_factor:.2f}" if r.is_report.profit_factor != float("inf") else "INF"
        oos_pf_str = f"{r.oos_report.profit_factor:.2f}" if r.oos_report.profit_factor != float("inf") else "INF"
        stat_color = status_colors.get(r.status, "white")

        table.add_row(
            str(idx),
            f"{p.z_threshold:.1f}",
            str(p.div_window),
            tp_sl_str,
            str(r.is_report.total_trades),
            f"{r.is_report.win_rate_pct:.1f}%",
            is_pf_str,
            f"{r.is_report.sharpe_ratio:.2f}",
            str(r.oos_report.total_trades),
            f"{r.oos_report.win_rate_pct:.1f}%",
            oos_pf_str,
            f"{r.oos_report.sharpe_ratio:.2f}",
            f"[{stat_color}]{r.status}[/{stat_color}]",
        )

    c.print(table)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Walk-Forward Parameter Sweep for SMC Crypto-Quant")
    parser.add_argument("--data-dir", type=pathlib.Path, default=_DEFAULT_DATA_DIR)
    parser.add_argument("--gex-file", type=pathlib.Path, default=_DEFAULT_GEX_FILE)
    parser.add_argument("--split-ratio", type=float, default=0.70, help="Train/test chronological split ratio")
    parser.add_argument("--balance", type=float, default=10_000.0)
    parser.add_argument("--position-usdt", type=float, default=1_000.0)
    parser.add_argument("--top-n", type=int, default=15)
    parser.add_argument("--export-json", type=pathlib.Path, default=None, help="Export sweep results to JSON")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if not args.gex_file.exists():
        _CONSOLE.print(f"[red]GEX snapshot not found: {args.gex_file}[/red]")
        return

    gex_map = load_gex_map_from_file(args.gex_file)
    if not gex_map:
        _CONSOLE.print(f"[red]GEX snapshot is empty: {args.gex_file}[/red]")
        return

    gex_manager = GexRiskManager(gex_map=gex_map)
    confluence = SuperConfluenceFilter()

    _CONSOLE.print("[dim]Loading merged tick dataset ...[/dim]")
    df = load_merged_frame(args.data_dir)

    grid = default_param_grid()
    _CONSOLE.print(
        f"[dim]Executing Walk-Forward sweep across {len(grid)} parameter sets "
        f"(Train={args.split_ratio * 100:.0f}%%, Test={(1 - args.split_ratio) * 100:.0f}%%) ...[/dim]\n"
    )

    base_config = TradeConfig(
        initial_balance=args.balance,
        position_size_usdt=args.position_usdt,
    )

    results = run_param_sweep(
        df=df,
        param_grid=grid,
        gex_manager=gex_manager,
        split_ratio=args.split_ratio,
        base_config=base_config,
        confluence=confluence,
    )

    _CONSOLE.print()
    render_sweep_table(results, top_n=args.top_n)

    if args.export_json:
        args.export_json.parent.mkdir(parents=True, exist_ok=True)
        export_data = [
            {
                "params": asdict(r.params),
                "status": r.status,
                "robustness_score": r.robustness_score,
                "is_report": asdict(r.is_report),
                "oos_report": asdict(r.oos_report),
            }
            for r in results
        ]
        args.export_json.write_text(json.dumps(export_data, indent=2), encoding="utf-8")
        _CONSOLE.print(f"[green]Results saved to {args.export_json}[/green]")


if __name__ == "__main__":
    main()
