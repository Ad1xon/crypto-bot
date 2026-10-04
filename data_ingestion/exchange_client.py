"""Binance WebSocket streaming client with integrated real-time SMC signal evaluation.

Extends the baseline exchange_client architecture with a :class:`LiveSignalEngine`
instance that runs the MicrostructureDetector + SuperConfluenceFilter (VWAP + GEX)
pipeline on every incoming depth-update tick.  All other behaviour — order-book
maintenance, CVD accumulation, TickLogger persistence, CLIDashboard rendering,
exponential-back-off reconnection — is identical to the original client.

Signal pipeline call-site
--------------------------
``_handle_orderbook_update`` calls :meth:`LiveSignalEngine.process_tick` after the
BBO snapshot has been enqueued.  Trade handlers call
:meth:`LiveSignalEngine.update_trade` so session VWAP stays in sync with CVD.
The engine's internal deque silently discards ticks until it has accumulated the
warm-up minimum (110 ticks ≈ 11 seconds at 100 ms cadence), so the first ~11
seconds of a new connection produce no signals and no confluence evaluations.
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

_DEPTH_EVENT_TYPE: Final[str] = "depthUpdate"
_TRADE_EVENT_TYPE: Final[str] = "aggTrade"


class BinanceStreamer:
    """Manages a persistent combined WebSocket stream from Binance.

    Responsibilities:
    - Connect to the Binance combined depth + aggTrade stream.
    - Maintain a local L2 order book via delta application.
    - Accumulate signed trade volume into a running CVD scalar.
    - Enqueue every trade tick and BBO snapshot into the TickLogger.
    - Dispatch each incoming message to the appropriate handler.
    - Drive CLIDashboard redraws on every depth update tick.
    - Evaluate SMC absorption signals through the LiveSignalEngine pipeline.
    - Reconnect automatically with exponential back-off on any failure.
    """

    def __init__(
        self,
        config: AppConfig = SETTINGS,
        dashboard: Optional[CLIDashboard] = None,
        execution_engine: Optional[ExecutionEngine] = None,
    ) -> None:
        self._config: AppConfig = config
        self._url: str = config.binance.combined_stream_url
        self._reconnect: ReconnectPolicy = config.reconnect
        self._running: bool = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._dashboard: Optional[CLIDashboard] = dashboard
        self._bids: dict[str, float] = {}
        self._asks: dict[str, float] = {}
        self._cvd: float = 0.0
        self._tick_logger: TickLogger = TickLogger(
            symbol=config.binance.symbol,
            config=config.tick_logger,
        )
        self._live_engine: LiveSignalEngine = LiveSignalEngine(
            detector=MicrostructureDetector(divergence_window=100),
            gex_manager=GexRiskManager(),
            execution_engine=execution_engine,
        )

    async def start(self) -> None:
        """Begin streaming; runs until :meth:`stop` is called."""
        self._running = True
        if self._dashboard is not None:
            self._dashboard.start()
        await self._tick_logger.start()
        logger.info("BinanceStreamer starting — url=%s", self._url)
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
            logger.info("BinanceStreamer — WebSocket closed by caller")

    async def _connection_loop(self) -> None:
        """Outer reconnect loop implementing exponential back-off."""
        attempt: int = 0
        delay: float = self._reconnect.initial_delay_s

        while self._running:
            try:
                attempt += 1
                logger.info(
                    "Connecting to Binance WebSocket (attempt %d) — url=%s",
                    attempt,
                    self._url,
                )
                async with websockets.connect(
                    self._url,
                    ping_interval=20,
                    ping_timeout=10,
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

    async def _receive_loop(
        self, ws: websockets.WebSocketClientProtocol
    ) -> None:
        """Read raw frames from *ws* and dispatch to handlers."""
        async for raw in ws:
            if not self._running:
                break
            try:
                payload: dict[str, Any] = json.loads(raw)
                self._dispatch(payload)
            except json.JSONDecodeError as exc:
                logger.error("Failed to deserialise message: %s", exc)

    def _dispatch(self, payload: dict[str, Any]) -> None:
        """Route a decoded payload to its dedicated handler."""
        stream: str = payload.get("stream", "")
        data: dict[str, Any] = payload.get("data", payload)
        event_type: str = data.get("e", "")

        if event_type == _DEPTH_EVENT_TYPE:
            self._handle_orderbook_update(data)
        elif event_type == _TRADE_EVENT_TYPE:
            self._handle_trade(data)
        else:
            logger.debug(
                "Unrecognised event type '%s' on stream '%s'", event_type, stream
            )

    def _apply_book_delta(
        self,
        bid_updates: list[list[str]],
        ask_updates: list[list[str]],
    ) -> None:
        """Apply a Binance depth delta to the local bid and ask dictionaries.

        Per the Binance API spec, a quantity of '0' means the price level
        must be removed from the local book.
        """
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

    def _sorted_bids(self) -> list[tuple[float, float]]:
        """Return bids sorted descending by price (best bid first)."""
        return sorted(
            ((float(p), v) for p, v in self._bids.items()),
            key=lambda x: x[0],
            reverse=True,
        )

    def _sorted_asks(self) -> list[tuple[float, float]]:
        """Return asks sorted ascending by price (best ask first)."""
        return sorted(
            ((float(p), v) for p, v in self._asks.items()),
            key=lambda x: x[0],
        )

    def _handle_orderbook_update(self, data: dict[str, Any]) -> None:
        """Apply an L2 delta, log the BBO snapshot, refresh the dashboard, and
        feed the live signal engine with the current microstructure state.

        Sorted bid/ask lists are computed once and reused for the TickLogger BBO
        record, the CLIDashboard update, and the LiveSignalEngine tick to avoid
        redundant sort operations on the same data.
        """
        bid_updates: list[list[str]] = data.get("b", [])
        ask_updates: list[list[str]] = data.get("a", [])

        self._apply_book_delta(bid_updates, ask_updates)

        sorted_bids: list[tuple[float, float]] = self._sorted_bids()
        sorted_asks: list[tuple[float, float]] = self._sorted_asks()

        best_bid: tuple[float, float] = sorted_bids[0] if sorted_bids else (0.0, 0.0)
        best_ask: tuple[float, float] = sorted_asks[0] if sorted_asks else (0.0, 0.0)
        ofi: float = best_bid[1] - best_ask[1]

        self._tick_logger.put_bbo({
            "timestamp_ms": data.get("E", 0),
            "best_bid_price": best_bid[0],
            "best_bid_qty": best_bid[1],
            "best_ask_price": best_ask[0],
            "best_ask_qty": best_ask[1],
            "spread": best_ask[0] - best_bid[0],
            "ofi": ofi,
        })

        logger.debug(
            "orderbook_update | bids_levels=%d | asks_levels=%d | ofi=%.6f",
            len(self._bids),
            len(self._asks),
            ofi,
        )

        if self._dashboard is not None:
            self._dashboard.update(sorted_bids, sorted_asks, self._cvd)

        self._live_engine.process_tick(
            timestamp_ms=data.get("E", 0),
            price=best_bid[0],
            cvd=self._cvd,
            ofi=ofi,
        )

    def _handle_trade(self, data: dict[str, Any]) -> None:
        """Parse an aggTrade payload, accumulate CVD, and log the trade tick.

        Binance aggTrade semantics:
        - ``m=True``  → buyer is the market maker → aggressive taker sell → negative delta.
        - ``m=False`` → buyer is the taker        → aggressive taker buy  → positive delta.
        """
        quantity: float = float(data.get("q", 0))
        is_buyer_maker: bool = bool(data.get("m", False))
        delta: float = -quantity if is_buyer_maker else quantity
        self._cvd += delta
        trade_price: float = float(data.get("p", 0))
        self._live_engine.update_trade(trade_price, quantity)

        self._tick_logger.put_trade({
            "timestamp_ms": data.get("T", 0),
            "price": trade_price,
            "qty": quantity,
            "side": "SELL" if is_buyer_maker else "BUY",
            "is_buyer_maker": is_buyer_maker,
        })

        logger.debug(
            "trade | is_buyer_maker=%s | qty=%.6f | delta=%.6f | cvd=%.6f",
            is_buyer_maker,
            quantity,
            delta,
            self._cvd,
        )
