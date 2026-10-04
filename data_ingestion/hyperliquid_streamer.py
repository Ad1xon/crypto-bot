"""Hyperliquid WebSocket streaming client with integrated SMC signal evaluation."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from config.settings import SETTINGS, AppConfig
from data_ingestion.interfaces import DataStreamer
from data_ingestion.tick_logger import TickLogger
from execution.interfaces import ExecutionEngine
from execution.live_engine import LiveSignalEngine
from quant_models.gex_risk_manager import GexRiskManager
from quant_models.smc_detectors import MicrostructureDetector
from visualizer.cli_dashboard import CLIDashboard

logger: logging.Logger = logging.getLogger(__name__)


class HyperliquidStreamer(DataStreamer):
    """Manages a persistent Hyperliquid L1 WebSocket stream."""

    def __init__(
        self,
        config: AppConfig = SETTINGS,
        dashboard: CLIDashboard | None = None,
        execution_engine: ExecutionEngine | None = None,
    ) -> None:
        self._config = config
        self._url = config.hyperliquid_ws.ws_url
        self._symbol = config.hyperliquid_ws.symbol
        self._reconnect = config.reconnect
        self._running = False
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._dashboard = dashboard
        self._cvd = 0.0

        self._tick_logger = TickLogger(
            symbol=self._symbol,
            config=config.tick_logger,
        )
        self._live_engine = LiveSignalEngine(
            detector=MicrostructureDetector(divergence_window=100),
            gex_manager=GexRiskManager(),
            execution_engine=execution_engine,
        )
        # Hyperliquid subscriptions
        self._subscribe_payload = json.dumps(
            {"method": "subscribe", "subscription": {"type": "l2Book", "coin": self._symbol}}
        )
        self._subscribe_trades = json.dumps(
            {"method": "subscribe", "subscription": {"type": "trades", "coin": self._symbol}}
        )

    async def start(self) -> None:
        """Begin streaming."""
        self._running = True
        if self._dashboard is not None:
            self._dashboard.start()
        await self._tick_logger.start()
        logger.info("HyperliquidStreamer starting — url=%s symbol=%s", self._url, self._symbol)
        try:
            await self._connection_loop()
        finally:
            if self._dashboard is not None:
                self._dashboard.stop()
            await self._tick_logger.stop()

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            await self._ws.close()

    async def _connection_loop(self) -> None:
        attempt = 0
        delay = self._reconnect.initial_delay_s

        while self._running:
            try:
                attempt += 1
                async with websockets.connect(self._url) as ws:
                    self._ws = ws
                    attempt = 0
                    delay = self._reconnect.initial_delay_s
                    await ws.send(self._subscribe_payload)
                    await ws.send(self._subscribe_trades)
                    await self._receive_loop(ws)
            except (ConnectionClosed, WebSocketException, OSError) as exc:
                logger.warning("Hyperliquid WebSocket lost: %s", exc)
            finally:
                self._ws = None

            if not self._running:
                break

            await asyncio.sleep(delay)
            delay = min(delay * self._reconnect.backoff_factor, self._reconnect.max_delay_s)

    async def _receive_loop(self, ws: websockets.WebSocketClientProtocol) -> None:
        async for raw in ws:
            if not self._running:
                break
            try:
                payload = json.loads(raw)
                self._dispatch(payload)
            except json.JSONDecodeError:
                pass

    def _dispatch(self, payload: dict[str, Any]) -> None:
        channel = payload.get("channel", "")
        if channel == "l2Book":
            self._handle_orderbook(payload.get("data", {}))
        elif channel == "trades":
            self._handle_trades(payload.get("data", []))

    def _handle_orderbook(self, data: dict[str, Any]) -> None:
        # Mock parsing logic for Hyperliquid L2 book
        levels = data.get("levels", [[]])
        if not levels or not levels[0]:
            return

        # Hyperliquid sends full snapshot for BBO usually, or full book on updates
        best_bid = (float(levels[0][0].get("px", 0)), float(levels[0][0].get("sz", 0))) if levels[0] else (0.0, 0.0)
        best_ask = (
            (float(levels[1][0].get("px", 0)), float(levels[1][0].get("sz", 0)))
            if len(levels) > 1 and levels[1]
            else (0.0, 0.0)
        )
        ofi = best_bid[1] - best_ask[1]

        self._tick_logger.put_bbo(
            {
                "timestamp_ms": data.get("time", 0),
                "best_bid_price": best_bid[0],
                "best_bid_qty": best_bid[1],
                "best_ask_price": best_ask[0],
                "best_ask_qty": best_ask[1],
                "spread": best_ask[0] - best_bid[0],
                "ofi": ofi,
            }
        )

        if self._dashboard is not None:
            self._dashboard.update([best_bid], [best_ask], self._cvd)

        self._live_engine.process_tick(
            timestamp_ms=data.get("time", 0),
            price=best_bid[0],
            cvd=self._cvd,
            ofi=ofi,
        )

    def _handle_trades(self, trades: list[dict[str, Any]]) -> None:
        for trade in trades:
            sz = float(trade.get("sz", 0))
            px = float(trade.get("px", 0))
            side = trade.get("dir", "Buy")

            delta = sz if side == "Buy" else -sz
            self._cvd += delta
            self._live_engine.update_trade(px, sz)

            self._tick_logger.put_trade(
                {
                    "timestamp_ms": trade.get("time", 0),
                    "price": px,
                    "qty": sz,
                    "side": side,
                    "is_buyer_maker": side == "Sell",
                }
            )
