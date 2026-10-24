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


def test_sample_size_label():
    from research.event_backtester import sample_size_label

    assert sample_size_label(0) == "INSUFFICIENT"
    assert sample_size_label(29) == "INSUFFICIENT"
    assert sample_size_label(30) == "LOW"
    assert sample_size_label(99) == "LOW"
    assert sample_size_label(100) == "OK"
    assert sample_size_label(250) == "OK"


def test_backtest_engine_slippage_and_fees():
    # 2 bps slippage, 0.00045 fee rate
    config = TradeConfig(
        initial_balance=10000.0,
        position_size_usdt=1000.0,
        take_profit_usdt=200.0,
        stop_loss_usdt=100.0,
        fee_rate=0.00045,
        slippage_bps=2.0,
        cooldown_ms=0,
    )
    signal_map = {1000: "BULLISH_ABSORPTION"}
    engine = BacktestEngine(config, signal_map, gex_manager=None, confluence=DummyConfluence())

    # Entry at 50,000 -> with 2 bps slippage, entry price = 50,000 * 1.0002 = 50,010.0
    engine.process_tick(DummyTick(1000, 50000.0, 50000.0))
    assert engine.has_open_position
    assert engine._entry_price == 50010.0

    # Trigger SL: SL price = 50,010 - 100 = 49,910.
    # At tick 2000, price breaches SL at 49,900.
    # SL is a market exit -> pays 2 bps slippage: 49,910 * (1 - 0.0002) = 49,900.018
    engine.process_tick(DummyTick(2000, 49900.0, 50000.0))
    assert not engine.has_open_position
    records = engine.trade_records
    assert len(records) == 1
    trade = records[0]
    assert trade.outcome == "SL"
    assert trade.net_pnl < 0
    # Consistency check: balance_after must equal initial_balance + sum(net_pnl)
    assert abs(engine.equity_curve[-1] - (config.initial_balance + trade.net_pnl)) < 1e-4


def test_backtest_engine_with_position_sizer():
    from quant_models.position_sizer import PositionSizer

    config = TradeConfig(
        initial_balance=10000.0,
        take_profit_usdt=200.0,
        stop_loss_usdt=100.0,
        fee_rate=0.0,
        slippage_bps=0.0,
        cooldown_ms=0,
        max_order_size_btc=0.05,  # clamp
    )
    sizer = PositionSizer()
    signal_map = {1000: "BULLISH_ABSORPTION"}
    engine = BacktestEngine(
        config,
        signal_map,
        gex_manager=None,
        confluence=DummyConfluence(),
        position_sizer=sizer,
    )

    engine.process_tick(DummyTick(1000, 50000.0, 50000.0))
    # Uncapped size would be 500 / 100 = 5.0 BTC, but capped at 0.05 BTC
    assert engine._position_btc == 0.05


def test_backtest_engine_with_gex_history():
    from quant_models.gex_risk_manager import GexHistory

    config = TradeConfig(
        initial_balance=10000.0,
        position_size_usdt=1000.0,
        take_profit_usdt=200.0,
        stop_loss_usdt=100.0,
        fee_rate=0.0,
        cooldown_ms=0,
    )
    # GEX snapshot valid at ts 1000 with positive GEX at 50,000
    history = GexHistory([(1000, {50000.0: 1.0})])
    signal_map = {1000: "BULLISH_ABSORPTION"}

    engine = BacktestEngine(
        config,
        signal_map,
        gex_manager=None,
        confluence=SuperConfluenceFilter(),
        gex_history=history,
    )

    engine.process_tick(DummyTick(1000, 50000.0, 50000.0))
    assert engine.has_open_position
