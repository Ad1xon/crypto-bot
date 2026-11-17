import unittest.mock

import pytest

from crypto_quant.config.settings import AppConfig
from crypto_quant.data_ingestion.hyperliquid_streamer import HyperliquidStreamer
from crypto_quant.data_ingestion.order_book import PyOrderBook

BOOK_DATA = {
    "coin": "BTC",
    "time": 123456789,
    "levels": [
        [{"px": "50000.0", "sz": "1.5", "n": 1}, {"px": "49999.0", "sz": "2.0", "n": 2}],
        [{"px": "50001.0", "sz": "0.5", "n": 1}, {"px": "50002.0", "sz": "3.0", "n": 1}],
    ],
}


@pytest.fixture(params=["default", "python"])
def streamer(request: pytest.FixtureRequest) -> HyperliquidStreamer:
    """Runs each test with the default book (C++ if installed) and with the Python fallback."""
    s = HyperliquidStreamer(config=AppConfig())
    if request.param == "python":
        s._book = PyOrderBook()
    s._live_engine = unittest.mock.Mock()
    s._tick_logger = unittest.mock.Mock()
    s._dashboard = unittest.mock.Mock()
    return s


def test_handle_orderbook_parsing(streamer: HyperliquidStreamer) -> None:
    streamer._handle_orderbook(BOOK_DATA)
    assert streamer.cvd == 0.0  # orderbook shouldn't change CVD

    bbo = streamer._tick_logger.put_bbo.call_args.args[0]
    assert bbo == {
        "timestamp_ms": 123456789,
        "best_bid_price": 50000.0,
        "best_bid_qty": 1.5,
        "best_ask_price": 50001.0,
        "best_ask_qty": 0.5,
        "spread": 1.0,
        "ofi": 1.0,  # top-of-book imbalance: 1.5 - 0.5
    }
    streamer._live_engine.process_tick.assert_called_once_with(timestamp_ms=123456789, price=50000.0, cvd=0.0, ofi=1.0)
    bids, asks, _ = streamer._dashboard.update.call_args.args
    assert bids == [(50000.0, 1.5), (49999.0, 2.0)]
    assert asks == [(50001.0, 0.5), (50002.0, 3.0)]


def test_malformed_orderbook_is_skipped(streamer: HyperliquidStreamer) -> None:
    streamer._handle_orderbook({"time": 1, "levels": [[{"sz": "1.0"}], []]})
    streamer._handle_orderbook({"time": 1, "levels": [[{"px": "abc", "sz": "1.0"}], []]})
    streamer._tick_logger.put_bbo.assert_not_called()
    streamer._live_engine.process_tick.assert_not_called()


def test_handle_trades_parsing(streamer: HyperliquidStreamer) -> None:
    data = [
        {"coin": "BTC", "dir": "Buy", "px": "50000.0", "sz": "1.0", "time": 123456789, "hash": "0x1"},
        {"coin": "BTC", "dir": "Sell", "px": "50000.0", "sz": "0.5", "time": 123456790, "hash": "0x2"},
    ]
    streamer._handle_trades(data)
    assert streamer.cvd == 0.5  # 1.0 (Buy) - 0.5 (Sell) = 0.5


def test_cvd_manual_and_session_reset(streamer: HyperliquidStreamer) -> None:
    # Day 1 trade: 2026-07-01 12:00:00 UTC (1782907200000 ms)
    ts_day1 = 1782907200000
    data_day1 = [{"coin": "BTC", "dir": "Buy", "px": "60000.0", "sz": "2.5", "time": ts_day1}]
    streamer._handle_trades(data_day1)
    assert streamer.cvd == 2.5

    # Manual reset test
    streamer.reset_cvd(0.0)
    assert streamer.cvd == 0.0

    # Add trade on Day 1 again
    streamer._handle_trades(data_day1)
    assert streamer.cvd == 2.5

    # Day 2 trade: 2026-07-02 00:01:00 UTC (1782950460000 ms) -> triggers auto session reset
    ts_day2 = 1782950460000
    data_day2 = [{"coin": "BTC", "dir": "Buy", "px": "60100.0", "sz": "1.0", "time": ts_day2}]
    streamer._handle_trades(data_day2)
    # CVD reset to 0.0, then +1.0 from Day 2 trade
    assert streamer.cvd == 1.0
