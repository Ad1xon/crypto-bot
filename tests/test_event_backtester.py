import pandas as pd

from quant_models.gex_risk_manager import GexRiskManager
from quant_models.super_confluence import ConfluenceVerdict, SuperConfluenceFilter
from research.event_backtester import (
    BacktestEngine,
    TradeConfig,
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
