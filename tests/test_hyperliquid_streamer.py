import pytest
from data_ingestion.hyperliquid_streamer import HyperliquidStreamer
from config.settings import AppConfig


def test_handle_orderbook_parsing():
    config = AppConfig()
    streamer = HyperliquidStreamer(config=config)
    
    # Mock data for orderbook
    data = {
        "coin": "BTC",
        "time": 123456789,
        "levels": [
            [
                {"px": "50000.0", "sz": "1.5", "n": 1},
                {"px": "49999.0", "sz": "2.0", "n": 2}
            ],
            [
                {"px": "50001.0", "sz": "0.5", "n": 1},
                {"px": "50002.0", "sz": "3.0", "n": 1}
            ]
        ]
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
        {"coin": "BTC", "dir": "Sell", "px": "50000.0", "sz": "0.5", "time": 123456790, "hash": "0x2"}
    ]
    
    import unittest.mock
    streamer._live_engine = unittest.mock.Mock()
    streamer._tick_logger = unittest.mock.Mock()
    
    streamer._handle_trades(data)
    assert streamer._cvd == 0.5  # 1.0 (Buy) - 0.5 (Sell) = 0.5
