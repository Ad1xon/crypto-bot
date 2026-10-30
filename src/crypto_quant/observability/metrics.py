"""Prometheus metrics exposed by the trading bot and the GEX collector.

All metrics live on the default ``prometheus_client`` registry and are scraped
from ``http://<host>:<METRICS_PORT>/metrics``. Naming follows the Prometheus
conventions: ``crypto_quant_`` namespace, base units (seconds, USDT, BTC) and a
``_total`` suffix for counters.

Label cardinality is kept bounded on purpose: every label value comes from a
small closed set (channel, verdict, outcome), never from prices or order ids.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from prometheus_client import Counter, Gauge, Histogram, Info, start_http_server

_LOG: Final[logging.Logger] = logging.getLogger(__name__)

NAMESPACE: Final[str] = "crypto_quant"

BUILD_INFO = Info(f"{NAMESPACE}_build", "Static build and runtime mode information.")

# --- Market data ------------------------------------------------------------
WS_CONNECTED = Gauge(f"{NAMESPACE}_ws_connected", "1 while the Hyperliquid WebSocket is connected.")
WS_RECONNECTS_TOTAL = Counter(f"{NAMESPACE}_ws_reconnects_total", "WebSocket disconnects followed by a reconnect.")
WS_MESSAGES_TOTAL = Counter(f"{NAMESPACE}_ws_messages_total", "WebSocket messages received.", ["channel"])
WS_LAST_MESSAGE_TS = Gauge(
    f"{NAMESPACE}_ws_last_message_timestamp_seconds",
    "Unix time of the last WebSocket message (staleness = time() - value).",
)
MID_PRICE = Gauge(f"{NAMESPACE}_mid_price_usd", "Best bid/ask mid price.")
SPREAD = Gauge(f"{NAMESPACE}_spread_usd", "Best ask minus best bid.")
CVD = Gauge(f"{NAMESPACE}_cvd_btc", "Session cumulative volume delta.")

# --- Signal pipeline --------------------------------------------------------
SIGNALS_TOTAL = Counter(
    f"{NAMESPACE}_signals_total",
    "Absorption signals detected, split by final verdict.",
    ["signal_type", "verdict", "reason"],
)
SIGNAL_EVAL_SECONDS = Histogram(
    f"{NAMESPACE}_signal_evaluation_seconds",
    "Wall time of one detector pass over the rolling window.",
    buckets=(0.0005, 0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25),
)

# --- Execution --------------------------------------------------------------
ORDERS_TOTAL = Counter(
    f"{NAMESPACE}_orders_total",
    "Entry orders by engine, side and result.",
    ["engine", "side", "status"],
)
TRADES_CLOSED_TOTAL = Counter(
    f"{NAMESPACE}_trades_closed_total",
    "Closed positions by engine and exit reason.",
    ["engine", "outcome"],
)
ORDER_LATENCY_SECONDS = Histogram(
    f"{NAMESPACE}_order_latency_seconds",
    "Signal detection to order acknowledgement latency.",
    ["engine"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
BALANCE = Gauge(f"{NAMESPACE}_balance_usdt", "Account balance reported by the execution engine.")
SESSION_PNL = Gauge(f"{NAMESPACE}_session_pnl_usdt", "Realised net PnL of the current session.")
POSITION_SIZE = Gauge(f"{NAMESPACE}_position_size_btc", "Signed open position (long > 0, short < 0).")
KILL_SWITCH_ACTIVE = Gauge(f"{NAMESPACE}_kill_switch_active", "1 when new entries are frozen by a risk guard.")

# --- Context filters --------------------------------------------------------
MACRO_BLACKOUT = Gauge(f"{NAMESPACE}_macro_blackout", "1 inside a high-impact macro event blackout window.")
SENTIMENT_NET = Gauge(f"{NAMESPACE}_sentiment_net_score", "FinBERT net sentiment score in [-1, 1].")
GEX_SNAPSHOT_AGE = Gauge(
    f"{NAMESPACE}_gex_snapshot_age_seconds", "Age of live_gex.json as seen by the bot (-1 if missing)."
)

# --- GEX collector (separate process) ----------------------------------------
GEX_LAST_SNAPSHOT_TS = Gauge(
    f"{NAMESPACE}_gex_last_snapshot_timestamp_seconds", "Unix time of the last successful Deribit snapshot."
)
GEX_FETCH_ERRORS_TOTAL = Counter(f"{NAMESPACE}_gex_fetch_errors_total", "Failed Deribit polls.", ["kind"])
GEX_FETCH_SECONDS = Histogram(
    f"{NAMESPACE}_gex_fetch_seconds",
    "Deribit book summary request duration.",
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0),
)
GEX_SPOT = Gauge(f"{NAMESPACE}_gex_spot_usd", "BTC index price reported with the last GEX snapshot.")
GEX_NET_TOTAL = Gauge(f"{NAMESPACE}_gex_net_total", "Sum of net GEX across all strikes.")
GEX_STRIKES = Gauge(f"{NAMESPACE}_gex_strikes", "Number of strikes in the last GEX snapshot.")


@dataclass(frozen=True)
class EngineSnapshot:
    """Point-in-time risk state of an execution engine, engine-agnostic."""

    position_btc: float = 0.0
    session_pnl_usdt: float = 0.0
    kill_switch_active: bool = False


def record_engine_snapshot(snapshot: EngineSnapshot, balance_usdt: float) -> None:
    """Publish engine risk state to the execution gauges."""
    BALANCE.set(balance_usdt)
    SESSION_PNL.set(snapshot.session_pnl_usdt)
    POSITION_SIZE.set(snapshot.position_btc)
    KILL_SWITCH_ACTIVE.set(1 if snapshot.kill_switch_active else 0)


def start_metrics_server(port: int, build_labels: dict[str, str] | None = None) -> bool:
    """Expose ``/metrics`` on ``port``. ``port <= 0`` disables the exporter.

    Returns True when the HTTP server was started. A bind failure is logged and
    swallowed: losing observability must never stop the trading loop.
    """
    if build_labels:
        BUILD_INFO.info(build_labels)
    if port <= 0:
        _LOG.info("Prometheus exporter disabled (METRICS_PORT=%s).", port)
        return False
    try:
        start_http_server(port)
    except OSError as exc:
        _LOG.error("Prometheus exporter failed to bind on port %d: %s", port, exc)
        return False
    _LOG.info("Prometheus exporter listening on :%d/metrics", port)
    return True
