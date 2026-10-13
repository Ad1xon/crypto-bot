import pandas as pd

from quant_models.gex_risk_manager import GexRiskManager
from quant_models.super_confluence import ConfluenceVerdict, SuperConfluenceFilter
from research.event_backtester import (
    BacktestEngine,
    TradeConfig,
    TradeRecord,
    compute_performance_report,
)


class DummyTick:
    def __init__(self, ts, price, vwap):
        self.timestamp_ms = ts
        self.price = price
        self.vwap = vwap
        self.dt = pd.to_datetime(ts, unit="ms", utc=True)


class DummyConfluence(SuperConfluenceFilter):
    def evaluate(self, signal_type, price, vwap, gex_map):
        return ConfluenceVerdict(allowed=True, vwap_ok=True, gex_ok=True, nearest_strike=50000.0, gex_value=1.0)


def test_compute_performance_report_empty():
    config = TradeConfig(initial_balance=1000.0)
    report = compute_performance_report([], [1000.0], config)
    assert report.total_trades == 0
    assert report.net_pnl == 0.0
    assert report.final_balance == 1000.0


def test_backtest_engine_basic_long_tp():
    config = TradeConfig(
        initial_balance=10000.0,
        position_size_usdt=1000.0,
        take_profit_usdt=100.0,  # 100 USDT TP
        stop_loss_usdt=50.0,
        fee_rate=0.0,  # zero fee for simplicity
        cooldown_ms=0,
    )
    gex = GexRiskManager(gex_map={})
    confluence = DummyConfluence()
    signal_map = {1000: "BULLISH_ABSORPTION"}

    engine = BacktestEngine(config, signal_map, gex, confluence)

    # Tick 1: Trigger signal
    engine.process_tick(DummyTick(1000, 50000.0, 50000.0))
    assert engine.has_open_position

    # Tick 2: Hit TP (entry + TP distance)
    engine.process_tick(DummyTick(2000, 50100.0, 50000.0))

    assert not engine.has_open_position
    records = engine.trade_records
    assert len(records) == 1
    assert records[0].outcome == "TP"
    assert records[0].net_pnl > 0
    assert records[0].exit_price == 50100.0


def test_backtest_engine_percentage_tp_sl():
    # 1% TP (500 pts at 50,000), 0.5% SL (250 pts)
    config = TradeConfig(
        initial_balance=10000.0,
        position_size_usdt=1000.0,
        tp_pct=0.01,
        sl_pct=0.005,
        fee_rate=0.0,
        cooldown_ms=0,
    )
    gex = GexRiskManager(gex_map={})
    confluence = DummyConfluence()
    signal_map = {1000: "BULLISH_ABSORPTION"}

    engine = BacktestEngine(config, signal_map, gex, confluence)

    # Tick 1: Trigger entry at 50,000
    engine.process_tick(DummyTick(1000, 50000.0, 50000.0))
    assert engine.has_open_position

    # Tick 2: Price goes to 50,500 (+1% TP hit)
    engine.process_tick(DummyTick(2000, 50500.0, 50000.0))
    assert not engine.has_open_position
    records = engine.trade_records
    assert len(records) == 1
    assert records[0].outcome == "TP"
    assert records[0].exit_price == 50500.0


def test_compute_performance_report_quant_metrics():
    config = TradeConfig(initial_balance=10000.0)
    now = pd.Timestamp.now(tz="UTC")

    r1 = TradeRecord(
        trade_id=1,
        direction="LONG",
        entry_time=now,
        exit_time=now,
        entry_price=50000.0,
        exit_price=50100.0,
        position_btc=0.02,
        gross_pnl=20.0,
        total_fees=2.0,
        net_pnl=18.0,
        outcome="TP",
        duration_s=60.0,
        balance_after=10018.0,
    )
    r2 = TradeRecord(
        trade_id=2,
        direction="SHORT",
        entry_time=now,
        exit_time=now,
        entry_price=50100.0,
        exit_price=50150.0,
        position_btc=0.02,
        gross_pnl=-10.0,
        total_fees=2.0,
        net_pnl=-12.0,
        outcome="SL",
        duration_s=40.0,
        balance_after=10006.0,
    )
    r3 = TradeRecord(
        trade_id=3,
        direction="LONG",
        entry_time=now,
        exit_time=now,
        entry_price=50000.0,
        exit_price=50200.0,
        position_btc=0.02,
        gross_pnl=40.0,
        total_fees=2.0,
        net_pnl=38.0,
        outcome="TP",
        duration_s=120.0,
        balance_after=10044.0,
    )

    records = [r1, r2, r3]
    equity_curve = [10000.0, 10018.0, 10006.0, 10044.0]
    report = compute_performance_report(records, equity_curve, config)

    assert report.total_trades == 3
    assert report.winning_trades == 2
    assert report.losing_trades == 1
    assert report.net_pnl == 44.0
    assert report.sharpe_ratio > 0.0
    assert report.sortino_ratio > 0.0
    assert report.calmar_ratio > 0.0
    assert report.win_loss_ratio > 1.0  # Avg win (28) / Avg loss (12) > 2
