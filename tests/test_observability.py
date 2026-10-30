"""Unit tests for metrics, healthcheck and logging used by the container runtime."""

from __future__ import annotations

import asyncio
import json
import logging
import pathlib
import time

from prometheus_client import REGISTRY

from crypto_quant.execution.live_engine import PaperTrader
from crypto_quant.observability import metrics
from crypto_quant.observability.healthcheck import evaluate, parse_gauge
from crypto_quant.observability.logging_setup import JsonFormatter

_METRICS_TEXT = """\
# HELP crypto_quant_ws_last_message_timestamp_seconds Unix time of the last WebSocket message.
# TYPE crypto_quant_ws_last_message_timestamp_seconds gauge
crypto_quant_ws_last_message_timestamp_seconds 1.7e+09
crypto_quant_ws_messages_total{channel="trades"} 42.0
"""


def _sample(name: str, labels: dict[str, str] | None = None) -> float:
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


# healthcheck


def test_parse_gauge_reads_unlabelled_value() -> None:
    assert parse_gauge(_METRICS_TEXT, "crypto_quant_ws_last_message_timestamp_seconds") == 1.7e9


def test_parse_gauge_missing_metric_returns_none() -> None:
    assert parse_gauge(_METRICS_TEXT, "does_not_exist") is None


def test_evaluate_healthy_when_fresh() -> None:
    healthy, reason = evaluate(_METRICS_TEXT, "crypto_quant_ws_last_message_timestamp_seconds", 120, 1.7e9 + 30)
    assert healthy
    assert reason.startswith("ok")


def test_evaluate_unhealthy_when_stale() -> None:
    healthy, reason = evaluate(_METRICS_TEXT, "crypto_quant_ws_last_message_timestamp_seconds", 120, 1.7e9 + 500)
    assert not healthy
    assert "stale" in reason


def test_evaluate_unhealthy_when_never_set() -> None:
    text = "crypto_quant_ws_last_message_timestamp_seconds 0.0\n"
    healthy, reason = evaluate(text, "crypto_quant_ws_last_message_timestamp_seconds", 120, time.time())
    assert not healthy
    assert "never" in reason


# metrics


def test_start_metrics_server_disabled_on_port_zero() -> None:
    assert metrics.start_metrics_server(0) is False


def test_record_engine_snapshot_sets_gauges() -> None:
    snap = metrics.EngineSnapshot(position_btc=-0.01, session_pnl_usdt=-12.5, kill_switch_active=True)
    metrics.record_engine_snapshot(snap, balance_usdt=9_987.5)

    assert _sample("crypto_quant_balance_usdt") == 9_987.5
    assert _sample("crypto_quant_session_pnl_usdt") == -12.5
    assert _sample("crypto_quant_position_size_btc") == -0.01
    assert _sample("crypto_quant_kill_switch_active") == 1.0


def test_paper_trader_snapshot_and_order_counter(tmp_path: pathlib.Path) -> None:
    trader = PaperTrader(sl_pct=0.002, tp_pct=0.004, cooldown_s=0.0, state_file=tmp_path / "state.json")
    labels = {"engine": "paper", "side": "sell", "status": "filled"}
    before = _sample("crypto_quant_orders_total", labels)

    asyncio.run(trader.execute_signal("BEARISH_ABSORPTION", 60_000.0, time.time()))

    snap = trader.risk_snapshot()
    assert snap.position_btc < 0  # short is negative
    assert snap.kill_switch_active is False
    assert _sample("crypto_quant_orders_total", labels) == before + 1


def test_paper_trader_snapshot_reports_kill_switch(tmp_path: pathlib.Path) -> None:
    trader = PaperTrader(max_daily_loss_usdt=100.0, state_file=tmp_path / "state.json")
    trader._session_pnl = -150.0
    assert trader.risk_snapshot().kill_switch_active is True


def test_streamer_dispatch_updates_heartbeat() -> None:
    from crypto_quant.config.settings import AppConfig
    from crypto_quant.data_ingestion.hyperliquid_streamer import HyperliquidStreamer

    streamer = HyperliquidStreamer(config=AppConfig())
    before = _sample("crypto_quant_ws_messages_total", {"channel": "trades"})
    streamer._dispatch({"channel": "trades", "data": []})

    assert _sample("crypto_quant_ws_messages_total", {"channel": "trades"}) == before + 1
    assert time.time() - _sample("crypto_quant_ws_last_message_timestamp_seconds") < 5


def test_gex_snapshot_age(tmp_path: pathlib.Path) -> None:
    from crypto_quant.main import _gex_snapshot_age_s

    assert _gex_snapshot_age_s(tmp_path / "missing.json") == -1.0

    f = tmp_path / "live_gex.json"
    f.write_text("{}", encoding="utf-8")
    age = _gex_snapshot_age_s(f, now_s=f.stat().st_mtime + 42.0)
    assert abs(age - 42.0) < 1e-6


# logging


def test_json_formatter_emits_valid_json() -> None:
    record = logging.LogRecord("crypto_quant.test", logging.WARNING, __file__, 1, "fill %s", ("ok",), None)
    payload = json.loads(JsonFormatter().format(record))

    assert payload["level"] == "WARNING"
    assert payload["logger"] == "crypto_quant.test"
    assert payload["msg"] == "fill ok"
    assert "ts" in payload
