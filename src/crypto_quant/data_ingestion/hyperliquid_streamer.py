"""Hyperliquid WebSocket streaming client with integrated SMC signal evaluation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterable, Sequence
from typing import Any

import websockets
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed, WebSocketException

from crypto_quant.config.settings import SETTINGS, AppConfig
from crypto_quant.data_ingestion.interfaces import DataStreamer
from crypto_quant.data_ingestion.order_book import OrderBookLike, create_order_book
from crypto_quant.data_ingestion.tick_logger import TickLogger
from crypto_quant.execution.interfaces import ExecutionEngine
from crypto_quant.execution.live_engine import LiveSignalEngine
from crypto_quant.observability import metrics
from crypto_quant.quant_models.gex_risk_manager import GexRiskManager
from crypto_quant.quant_models.macro_filter import MacroCalendarFilter
from crypto_quant.quant_models.sentiment_analyzer import CryptoSentimentAnalyzer
from crypto_quant.quant_models.smc_detectors import MicrostructureDetector
from crypto_quant.visualizer.cli_dashboard import CLIDashboard

logger: logging.Logger = logging.getLogger(__name__)


def _as_tuples(rows: Iterable[Sequence[float]]) -> list[tuple[float, float]]:
    return [(float(px), float(sz)) for px, sz in rows]


class HyperliquidStreamer(DataStreamer):
    """Manages a persistent Hyperliquid L1 WebSocket stream."""

    def __init__(
        self,
        config: AppConfig = SETTINGS,
        dashboard: CLIDashboard | None = None,
        execution_engine: ExecutionEngine | None = None,
        macro_filter: MacroCalendarFilter | None = None,
        sentiment_analyzer: CryptoSentimentAnalyzer | None = None,
        auto_reset_cvd: bool = True,
    ) -> None:
        self._config = config
        self._url = config.hyperliquid_ws.ws_url
        self._symbol = config.hyperliquid_ws.symbol
        self._reconnect = config.reconnect
        self._running = False
        self._ws: ClientConnection | None = None
        self._dashboard = dashboard
        self._book: OrderBookLike = create_order_book(auto_reset_cvd=auto_reset_cvd)

        self._tick_logger = TickLogger(
            symbol=self._symbol,
            config=config.tick_logger,
        )
        self._live_engine = LiveSignalEngine(
            detector=MicrostructureDetector(divergence_window=100),
            gex_manager=GexRiskManager(),
            execution_engine=execution_engine,
            macro_filter=macro_filter,
            sentiment_analyzer=sentiment_analyzer,
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
                    self._live_engine.clear_window()
                    metrics.WS_CONNECTED.set(1)
                    await self._receive_loop(ws)
            except (ConnectionClosed, WebSocketException, OSError) as exc:
                logger.warning("Hyperliquid WebSocket lost: %s", exc)
            finally:
                self._ws = None
                metrics.WS_CONNECTED.set(0)

            if not self._running:
                break

            metrics.WS_RECONNECTS_TOTAL.inc()
            await asyncio.sleep(delay)
            delay = min(delay * self._reconnect.backoff_factor, self._reconnect.max_delay_s)

    async def _receive_loop(self, ws: ClientConnection) -> None:
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
        if channel in ("l2Book", "trades"):
            metrics.WS_MESSAGES_TOTAL.labels(channel=channel).inc()
            metrics.WS_LAST_MESSAGE_TS.set(time.time())
        if channel == "l2Book":
            self._handle_orderbook(payload.get("data", {}))
        elif channel == "trades":
            self._handle_trades(payload.get("data", []))

    @property
    def cvd(self) -> float:
        return self._book.cvd

    def _handle_orderbook(self, data: dict[str, Any]) -> None:
        levels = data.get("levels", [[], []])
        if not levels or not levels[0]:
            return

        ts_ms = int(data.get("time", 0))
        book = self._book
        try:
            book.update_hyperliquid(levels, ts_ms)
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("Skipping malformed l2Book message: %s", exc)
            return

        # Still bid size - ask size at the top level, so the detector sees the same "ofi" as before.
        ofi = book.top_imbalance
        cvd = book.cvd
        if book.is_two_sided:
            metrics.MID_PRICE.set(book.mid_price)
            metrics.SPREAD.set(book.spread)
        metrics.CVD.set(cvd)

        self._tick_logger.put_bbo(
            {
                "timestamp_ms": ts_ms,
                "best_bid_price": book.best_bid_price,
                "best_bid_qty": book.best_bid_size,
                "best_ask_price": book.best_ask_price,
                "best_ask_qty": book.best_ask_size,
                "spread": book.spread,
                "ofi": ofi,
            }
        )

        if self._dashboard is not None:
            self._dashboard.update(_as_tuples(book.bids()), _as_tuples(book.asks()), cvd)

        self._live_engine.process_tick(
            timestamp_ms=ts_ms,
            price=book.best_bid_price,
            cvd=cvd,
            ofi=ofi,
        )

    def reset_cvd(self, initial_value: float = 0.0) -> None:
        """Reset cumulative volume delta to prevent multi-day unbounded drift."""
        logger.info("Resetting CVD from %.4f to %.4f", self._book.cvd, initial_value)
        self._book.reset_cvd(initial_value)

    def _handle_trades(self, trades: list[dict[str, Any]]) -> None:
        for trade in trades:
            ts_ms = int(trade.get("time", 0))
            sz = float(trade.get("sz", 0))
            px = float(trade.get("px", 0))
            raw_side = trade.get("side") or trade.get("dir") or "B"
            is_buy: bool = raw_side in ("B", "Buy", "BUY")
            side: str = "Buy" if is_buy else "Sell"

            # CVD resets on the first trade of a new UTC day (handled inside the order book).
            self._book.apply_trade(px, sz, is_buy, ts_ms)
            self._live_engine.update_trade(px, sz)

            self._tick_logger.put_trade(
                {
                    "timestamp_ms": ts_ms,
                    "price": px,
                    "qty": sz,
                    "side": side,
                    "is_buyer_maker": not is_buy,
                }
            )
