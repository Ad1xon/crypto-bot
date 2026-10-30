import pandas as pd
from rich.console import Console

from crypto_quant.quant_models.gex_risk_manager import GexRiskManager
from crypto_quant.quant_models.super_confluence import ConfluenceVerdict, SuperConfluenceFilter
from crypto_quant.research.event_backtester import PerformanceReport, TradeConfig
from crypto_quant.research.param_sweep import (
    SweepParams,
    SweepResult,
    default_param_grid,
    evaluate_robustness,
    render_sweep_table,
    run_param_sweep,
)


class AlwaysAllowedConfluence(SuperConfluenceFilter):
    def evaluate(self, signal_type, price, vwap, gex_map):
        return ConfluenceVerdict(allowed=True, vwap_ok=True, gex_ok=True, nearest_strike=50000.0, gex_value=0.0)


def _make_dummy_report(trades: int, pf: float, net_pnl: float, sharpe: float) -> PerformanceReport:
    return PerformanceReport(
        initial_balance=10000.0,
        final_balance=10000.0 + net_pnl,
        total_trades=trades,
        winning_trades=trades // 2,
        losing_trades=trades - (trades // 2),
        win_rate_pct=50.0,
        gross_profit=max(net_pnl, 0.0),
        gross_loss=abs(min(net_pnl, 0.0)),
        profit_factor=pf,
        net_pnl=net_pnl,
        return_pct=net_pnl / 100.0,
        max_drawdown_usdt=10.0,
        max_drawdown_pct=0.1,
        avg_win_usdt=10.0,
        avg_loss_usdt=-5.0,
        avg_duration_s=60.0,
        sharpe_ratio=sharpe,
        sortino_ratio=sharpe * 1.2,
        calmar_ratio=1.5,
        win_loss_ratio=2.0,
    )


def test_evaluate_robustness():
    # Insufficient trades
    score, status = evaluate_robustness(_make_dummy_report(1, 2.0, 50.0, 1.0), _make_dummy_report(1, 2.0, 50.0, 1.0))
    assert status == "INSUFFICIENT_TRADES"

    # Overfit: good IS, negative or PF < 1 OOS
    score, status = evaluate_robustness(
        _make_dummy_report(10, 2.5, 500.0, 1.8), _make_dummy_report(10, 0.8, -50.0, -0.5)
    )
    assert status == "OVERFIT"

    # Robust: high persistence OOS
    score, status = evaluate_robustness(
        _make_dummy_report(10, 2.0, 400.0, 1.5), _make_dummy_report(10, 1.8, 300.0, 1.2)
    )
    assert status == "ROBUST"
    assert score > 0.7

    # Degraded: OOS profitable but PF fell below 70% of IS
    score, status = evaluate_robustness(_make_dummy_report(10, 3.0, 500.0, 2.0), _make_dummy_report(10, 1.2, 50.0, 0.4))
    assert status == "DEGRADED"


def test_default_param_grid():
    grid = default_param_grid()
    assert len(grid) == 27  # 3 * 3 * 3
    assert all(isinstance(p, SweepParams) for p in grid)
    assert grid[0].z_threshold == 1.5


def test_run_param_sweep_small_synthetic():
    # Synthetic dataset of 120 ticks
    timestamps = [1000 + i * 100 for i in range(120)]
    prices = [50000.0 + (i % 5) * 10.0 for i in range(120)]
    cvd = [float(i * 2) for i in range(120)]
    ofi = [1.0 if i % 2 == 0 else -1.0 for i in range(120)]
    vwap = [50000.0 for _ in range(120)]
    dt = pd.to_datetime(timestamps, unit="ms", utc=True)

    df = pd.DataFrame(
        {
            "timestamp_ms": timestamps,
            "price": prices,
            "cvd": cvd,
            "ofi": ofi,
            "vwap": vwap,
            "dt": dt,
        }
    )

    gex_manager = GexRiskManager(gex_map={})
    confluence = AlwaysAllowedConfluence()
    test_grid = [
        SweepParams(z_threshold=2.0, div_window=20, tp_pct=0.01, sl_pct=0.005),
    ]

    results = run_param_sweep(
        df=df,
        param_grid=test_grid,
        gex_manager=gex_manager,
        split_ratio=0.7,
        base_config=TradeConfig(initial_balance=5000.0),
        confluence=confluence,
    )

    assert len(results) == 1
    res = results[0]
    assert isinstance(res, SweepResult)
    assert res.is_report.initial_balance == 5000.0
    assert res.oos_report.initial_balance == 5000.0


def test_render_sweep_table_no_throw():
    console = Console(record=True, width=180)
    report = _make_dummy_report(10, 1.8, 200.0, 1.4)
    res = SweepResult(
        params=SweepParams(z_threshold=2.0, div_window=50, tp_pct=0.006, sl_pct=0.003),
        is_report=report,
        oos_report=report,
        robustness_score=1.0,
        status="ROBUST",
    )
    render_sweep_table([res], top_n=5, console=console)
    output = console.export_text()
    assert "ROBUST" in output
    assert "2.0" in output


def test_evaluate_robustness_custom_min_trades():
    # 15 trades with default min_trades=2 -> ROBUST
    rep = _make_dummy_report(15, 2.0, 300.0, 1.5)
    score, status = evaluate_robustness(rep, rep, min_trades=2)
    assert status == "ROBUST"

    # 15 trades with min_trades=30 -> INSUFFICIENT_TRADES
    score, status = evaluate_robustness(rep, rep, min_trades=30)
    assert status == "INSUFFICIENT_TRADES"
    assert score == 0.0
