"""Bybit V5 Linear Perpetuals WebSocket streaming client with integrated SMC signal evaluation.

Implements the same resilient architecture as :class:`BinanceStreamer` — asyncio event loop,
exponential back-off reconnection, TickLogger persistence, CLIDashboard rendering, and
LiveSignalEngine signal dispatch — but targets the Bybit V5 public linear stream.

The Bybit protocol requires an explicit application-level ping (``{"op": "ping"}``) at
least once per 20 seconds.  A dedicated :meth:`_ping_loop` coroutine runs concurrently
with the receive loop for every active connection, entirely separate from the WebSocket
library's protocol-level ping machinery which is disabled to avoid conflicts.

Orderbook state is maintained as a local L2 book.  ``snapshot`` messages atomically
replace the full book; ``delta`` messages apply price-level mutations where a quantity
of ``"0"`` signals deletion.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Final, Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from config.settings import AppConfig, ReconnectPolicy, SETTINGS
from data_ingestion.tick_logger import TickLogger
from execution.interfaces import ExecutionEngine
from execution.live_engine import LiveSignalEngine
from quant_models.gex_risk_manager import GexRiskManager
from quant_models.smc_detectors import MicrostructureDetector
from visualizer.cli_dashboard import CLIDashboard

logger: logging.Logger = logging.getLogger(__name__)

_PING_INTERVAL_S: Final[float] = 20.0
_MSG_TYPE_SNAPSHOT: Final[str] = "snapshot"
_MSG_TYPE_DELTA: Final[str] = "delta"
_TOPIC_ORDERBOOK: Final[str] = "orderbook"
_TOPIC_TRADE: Final[str] = "publicTrade"


class BybitStreamer:
    """Manages a persistent Bybit V5 Linear Perpetuals combined WebSocket stream.

    Responsibilities:
    - Connect to the Bybit V5 public linear WebSocket endpoint.
    - Subscribe to the L2 orderbook (depth 50) and public trade streams.
    - Maintain a local L2 order book via snapshot replacement and delta application.
    - Accumulate signed trade volume into a running CVD scalar.
    - Enqueue every trade tick and BBO snapshot into the TickLogger.
    - Dispatch each incoming message to the appropriate handler.
    - Drive CLIDashboard redraws on every orderbook update.
    - Evaluate SMC absorption signals through the LiveSignalEngine pipeline.
    - Send application-level pings every 20 seconds via a dedicated coroutine.
    - Reconnect automatically with exponential back-off on any failure.
    """

    def __init__(
        self,
        config: AppConfig = SETTINGS,
        dashboard: Optional[CLIDashboard] = None,
        execution_engine: Optional[ExecutionEngine] = None,
    ) -> None:
        self._config: AppConfig = config
        self._url: str = config.bybit_ws.ws_url
        self._symbol: str = config.bybit_ws.symbol
        self._reconnect: ReconnectPolicy = config.reconnect
        self._running: bool = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._dashboard: Optional[CLIDashboard] = dashboard
        self._bids: dict[str, float] = {}
        self._asks: dict[str, float] = {}
        self._cvd: float = 0.0
        self._tick_logger: TickLogger = TickLogger(
            symbol=config.bybit_ws.symbol,
            config=config.tick_logger,
        )
        self._live_engine: LiveSignalEngine = LiveSignalEngine(
            detector=MicrostructureDetector(divergence_window=100),
            gex_manager=GexRiskManager(),
            execution_engine=execution_engine,
        )
        self._subscribe_payload: str = json.dumps({
            "op": "subscribe",
            "args": [
                f"orderbook.50.{self._symbol}",
                f"publicTrade.{self._symbol}",
            ],
        })

    async def start(self) -> None:
        """Begin streaming; runs until :meth:`stop` is called."""
        self._running = True
        if self._dashboard is not None:
            self._dashboard.start()
        await self._tick_logger.start()
        logger.info("BybitStreamer starting — url=%s symbol=%s", self._url, self._symbol)
        try:
            await self._connection_loop()
        finally:
            if self._dashboard is not None:
                self._dashboard.stop()
            await self._tick_logger.stop()

    async def stop(self) -> None:
        """Signal the streamer to stop and close any open socket."""
        self._running = False
        if self._ws is not None:
            await self._ws.close()
            logger.info("BybitStreamer — WebSocket closed by caller")

    async def _connection_loop(self) -> None:
        """Outer reconnect loop implementing exponential back-off."""
        attempt: int = 0
        delay: float = self._reconnect.initial_delay_s

        while self._running:
            try:
                attempt += 1
                logger.info(
                    "Connecting to Bybit WebSocket (attempt %d) — url=%s",
                    attempt,
                    self._url,
                )
                async with websockets.connect(
                    self._url,
                    ping_interval=None,
                    ping_timeout=None,
                ) as ws:
                    self._ws = ws
                    attempt = 0
                    delay = self._reconnect.initial_delay_s
                    logger.info("Connection established — streaming")
                    await self._receive_loop(ws)
            except (ConnectionClosed, WebSocketException) as exc:
                logger.warning("WebSocket connection lost: %s", exc)
            except OSError as exc:
                logger.error("Network error: %s", exc)
            finally:
                self._ws = None

            if not self._running:
                break

            if attempt > self._reconnect.max_attempts:
                logger.critical(
                    "Exceeded max reconnect attempts (%d). Giving up.",
                    self._reconnect.max_attempts,
                )
                break

            logger.info("Reconnecting in %.1f seconds…", delay)
            await asyncio.sleep(delay)
            delay = min(delay * self._reconnect.backoff_factor, self._reconnect.max_delay_s)

    async def _receive_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Subscribe, launch the ping task, then read frames until disconnection."""
        await ws.send(self._subscribe_payload)
        logger.info("Subscription request sent for symbol=%s", self._symbol)

        ping_task: asyncio.Task[None] = asyncio.create_task(
            self._ping_loop(ws), name="bybit_ping"
        )
        try:
            async for raw in ws:
                if not self._running:
                    break
                try:
                    payload: dict[str, Any] = json.loads(raw)
                    self._dispatch(payload)
                except json.JSONDecodeError as exc:
                    logger.error("Failed to deserialise message: %s", exc)
        finally:
            ping_task.cancel()
            await asyncio.gather(ping_task, return_exceptions=True)

    async def _ping_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        """Send an application-level ping frame every 20 seconds.

        Bybit closes the connection if no ping is received within 20 seconds.
        This coroutine is cancelled automatically when the receive loop exits.
        """
        while True:
            await asyncio.sleep(_PING_INTERVAL_S)
            await ws.send(json.dumps({"op": "ping"}))
            logger.debug("Bybit ping sent")

    def _dispatch(self, payload: dict[str, Any]) -> None:
        """Route a decoded payload to its dedicated handler.

        Control frames (ping/pong acknowledgements, subscription confirmations)
        carry no ``topic`` field and are silently discarded.
        """
        topic: str = payload.get("topic", "")
        if not topic:
            return

        msg_type: str = payload.get("type", "")
        data: Any = payload.get("data", {})
        timestamp_ms: int = payload.get("ts", 0)

        if topic.startswith(_TOPIC_ORDERBOOK):
            self._handle_orderbook_update(data, msg_type, timestamp_ms)
        elif topic.startswith(_TOPIC_TRADE):
            self._handle_trade(data)
        else:
            logger.debug("Unrecognised topic '%s'", topic)

    def _handle_orderbook_update(
        self,
        data: dict[str, Any],
        msg_type: str,
        timestamp_ms: int,
    ) -> None:
        """Maintain the local L2 book, compute BBO and OFI, and feed downstream consumers.

        A ``snapshot`` clears the local book before applying the received levels,
        guaranteeing full consistency on (re)connection.  A ``delta`` applies only
        the changed levels; a quantity of ``"0"`` signals price-level deletion.

        Sorted bid/ask lists are computed once and reused across the TickLogger BBO
        record, the CLIDashboard update, and the LiveSignalEngine tick to avoid
        redundant sort operations on the same data.
        """
        if msg_type == _MSG_TYPE_SNAPSHOT:
            self._bids.clear()
            self._asks.clear()

        bid_updates: list[list[str]] = data.get("b", [])
        ask_updates: list[list[str]] = data.get("a", [])

        for price_str, qty_str in bid_updates:
            qty: float = float(qty_str)
            if qty == 0.0:
                self._bids.pop(price_str, None)
            else:
                self._bids[price_str] = qty

        for price_str, qty_str in ask_updates:
            qty = float(qty_str)
            if qty == 0.0:
                self._asks.pop(price_str, None)
            else:
                self._asks[price_str] = qty

        sorted_bids: list[tuple[float, float]] = sorted(
            ((float(p), v) for p, v in self._bids.items()),
            key=lambda x: x[0],
            reverse=True,
        )
        sorted_asks: list[tuple[float, float]] = sorted(
            ((float(p), v) for p, v in self._asks.items()),
            key=lambda x: x[0],
        )

        best_bid: tuple[float, float] = sorted_bids[0] if sorted_bids else (0.0, 0.0)
        best_ask: tuple[float, float] = sorted_asks[0] if sorted_asks else (0.0, 0.0)
        ofi: float = best_bid[1] - best_ask[1]

        self._tick_logger.put_bbo({
            "timestamp_ms": timestamp_ms,
            "best_bid_price": best_bid[0],
            "best_bid_qty": best_bid[1],
            "best_ask_price": best_ask[0],
            "best_ask_qty": best_ask[1],
            "spread": best_ask[0] - best_bid[0],
            "ofi": ofi,
        })

        logger.debug(
            "orderbook_update | type=%s | bids_levels=%d | asks_levels=%d | ofi=%.6f",
            msg_type,
            len(self._bids),
            len(self._asks),
            ofi,
        )

        if self._dashboard is not None:
            self._dashboard.update(sorted_bids, sorted_asks, self._cvd)

        self._live_engine.process_tick(
            timestamp_ms=timestamp_ms,
            price=best_bid[0],
            cvd=self._cvd,
            ofi=ofi,
        )

    def _handle_trade(self, trades: list[dict[str, Any]]) -> None:
        """Iterate over a batch of public trades, accumulate CVD, and log each tick.

        Bybit ``S`` field encodes the taker's aggressor side.  A taker ``"Buy"``
        represents an aggressive market buy (positive delta); a taker ``"Sell"``
        represents an aggressive market sell (negative delta).
        """
        for trade in trades:
            price: float = float(trade.get("p", 0))
            size: float = float(trade.get("v", 0))
            side: str = trade.get("S", "")
            timestamp_ms: int = trade.get("T", 0)

            delta: float = size if side == "Buy" else -size
            self._cvd += delta
            self._live_engine.update_trade(price, size)

            self._tick_logger.put_trade({
                "timestamp_ms": timestamp_ms,
                "price": price,
                "qty": size,
                "side": side.upper(),
                "is_buyer_maker": side == "Sell",
            })

            logger.debug(
                "trade | side=%s | qty=%.6f | delta=%.6f | cvd=%.6f",
                side,
                size,
                delta,
                self._cvd,
            )
