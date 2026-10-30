from crypto_quant.config.settings import AppConfig
from crypto_quant.data_ingestion.hyperliquid_streamer import HyperliquidStreamer


def test_handle_orderbook_parsing():
    config = AppConfig()
    streamer = HyperliquidStreamer(config=config)

    # Mock data for orderbook
    data = {
        "coin": "BTC",
        "time": 123456789,
        "levels": [
            [{"px": "50000.0", "sz": "1.5", "n": 1}, {"px": "49999.0", "sz": "2.0", "n": 2}],
            [{"px": "50001.0", "sz": "0.5", "n": 1}, {"px": "50002.0", "sz": "3.0", "n": 1}],
        ],
    }

    # Mock the internal components that do IO or require event loops
    import unittest.mock

    streamer._live_engine = unittest.mock.Mock()
    streamer._tick_logger = unittest.mock.Mock()
    streamer._dashboard = unittest.mock.Mock()

    streamer._handle_orderbook(data)
    assert streamer._cvd == 0.0  # orderbook shouldn't change CVD


def test_handle_trades_parsing():
    config = AppConfig()
    streamer = HyperliquidStreamer(config=config)

    data = [
        {"coin": "BTC", "dir": "Buy", "px": "50000.0", "sz": "1.0", "time": 123456789, "hash": "0x1"},
        {"coin": "BTC", "dir": "Sell", "px": "50000.0", "sz": "0.5", "time": 123456790, "hash": "0x2"},
    ]

    import unittest.mock

    streamer._live_engine = unittest.mock.Mock()
    streamer._tick_logger = unittest.mock.Mock()

    streamer._handle_trades(data)
    assert streamer._cvd == 0.5  # 1.0 (Buy) - 0.5 (Sell) = 0.5


def test_cvd_manual_and_session_reset():
    config = AppConfig()
    streamer = HyperliquidStreamer(config=config, auto_reset_cvd=True)

    import unittest.mock

    streamer._live_engine = unittest.mock.Mock()
    streamer._tick_logger = unittest.mock.Mock()

    # Day 1 trade: 2026-07-01 12:00:00 UTC (1782907200000 ms)
    ts_day1 = 1782907200000
    data_day1 = [{"coin": "BTC", "dir": "Buy", "px": "60000.0", "sz": "2.5", "time": ts_day1}]
    streamer._handle_trades(data_day1)
    assert streamer._cvd == 2.5

    # Manual reset test
    streamer.reset_cvd(0.0)
    assert streamer._cvd == 0.0

    # Add trade on Day 1 again
    streamer._handle_trades(data_day1)
    assert streamer._cvd == 2.5

    # Day 2 trade: 2026-07-02 00:01:00 UTC (1782950460000 ms) -> triggers auto session reset
    ts_day2 = 1782950460000
    data_day2 = [{"coin": "BTC", "dir": "Buy", "px": "60100.0", "sz": "1.0", "time": ts_day2}]
    streamer._handle_trades(data_day2)
    # CVD reset to 0.0, then +1.0 from Day 2 trade
    assert streamer._cvd == 1.0
