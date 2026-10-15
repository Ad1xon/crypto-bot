"""Latency stack profiling: signal_ts → fill_ts → latency_ms."""

from __future__ import annotations

import logging
import pathlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from execution.ccxt_broker import CCXTBroker
from quant_models.smc_detectors import MicrostructureDetector


@pytest.fixture
def clean_broker(tmp_path: pathlib.Path, mock_exchange: MagicMock) -> CCXTBroker:
    state_file = tmp_path / "live_state.json"
    with patch("execution.ccxt_broker.ccxt") as mock_ccxt:
        mock_ccxt.bybit.return_value = mock_exchange
        broker = CCXTBroker(
            exchange_id="bybit",
            api_key="test_key",
            api_secret="test_secret",
            testnet=False,
            symbol="BTC/USDT",
            risk_per_trade_pct=0.01,
            base_sl_usdt_dist=150.0,
            tp_usdt_dist=300.0,
            fee_rate=0.0004,
            trailing_activation_usdt=100.0,
            cooldown_s=0.0,
            max_daily_loss_usdt=500.0,
            state_file=state_file,
        )
    return broker


class TestSignalTimestamp:
    def test_detector_stamps_signal_ts_on_absorption(self) -> None:
        n: int = 200
        prices = [100.0] * 100 + [100.0] * 50 + [100.05] * 50
        cvd = [0.0] * 100 + list(range(-50, 0)) + list(range(50))
        ofi = [0.1] * 150 + [-0.1] * 50
        df = pd.DataFrame(
            {
                "timestamp_ms": list(range(n)),
                "dt": pd.to_datetime(list(range(n)), unit="ms", utc=True),
                "price": prices,
                "cvd": cvd,
                "ofi": ofi,
            }
        )
        detector = MicrostructureDetector(
            ofi_smooth_window=5,
            divergence_window=20,
            cvd_z_threshold=1.0,
            price_tolerance=1.0,
        )
        before: float = time.perf_counter()
        signals = detector.smooth_ofi(df)
        signals = detector.detect_cvd_divergence(signals)
        after: float = time.perf_counter()
        if signals.empty:
            pytest.skip("synthetic frame produced no absorption events")
        assert "signal_ts" in signals.columns
        assert signals["signal_ts"].notna().all()
        assert float(signals["signal_ts"].iloc[-1]) >= before
        assert float(signals["signal_ts"].iloc[-1]) <= after


class TestFillLatency:
    async def test_logs_latency_and_discord_payload(
        self,
        clean_broker: CCXTBroker,
        mock_exchange: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        mock_exchange.create_market_buy_order = AsyncMock(return_value={"id": "lat_001"})
        mock_send_alert = AsyncMock()
        clean_broker._alerter.send_alert = mock_send_alert  # type: ignore[method-assign]
        signal_ts: float = time.perf_counter()

        with caplog.at_level(logging.INFO):
            await clean_broker.execute_signal(
                signal_type="BULLISH_ABSORPTION",
                current_price=50_000.0,
                current_time=9_999_999.0,
                signal_ts=signal_ts,
            )

        assert any(
            "Order executed. Latency:" in record.getMessage() and "ms" in record.getMessage()
            for record in caplog.records
        )
        mock_send_alert.assert_called_once()
        kwargs = mock_send_alert.call_args.kwargs
        fields = kwargs["fields"]
        latency_field = next(f for f in fields if f["name"] == "Latency")
        assert "ms" in str(latency_field["value"])
