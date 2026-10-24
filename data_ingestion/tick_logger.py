"""Non-blocking, queue-backed tick-data logger for trade and BBO records.

Records arrive on the hot WebSocket path via put_nowait and are buffered in
bounded asyncio.Queues.  Background flush workers drain each queue into
pandas DataFrames and persist them as Parquet files using asyncio.to_thread
so that blocking disk I/O never stalls the event loop.

Flush is triggered by whichever condition arrives first:
- The in-memory batch reaches ``batch_size`` records.
- ``flush_interval_s`` seconds elapse with a non-empty batch.

On graceful shutdown a sentinel value is pushed into each queue, causing
each worker to flush its remaining batch before exiting.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pandas as pd

from config.settings import TickLoggerConfig

logger: logging.Logger = logging.getLogger(__name__)

_SENTINEL: Final[None] = None

TradeRecord = dict[str, Any]
BBORecord = dict[str, Any]


class TickLogger:
    """Asynchronous, non-blocking logger for tick-level market data.

    Responsibilities:
    - Own two bounded asyncio.Queues: one for trades, one for BBO snapshots.
    - Launch background flush workers on start().
    - Expose synchronous put_trade() / put_bbo() for the hot WebSocket path.
    - Offload blocking Parquet I/O to a thread pool via asyncio.to_thread.
    - Drain and flush all remaining records to disk on graceful stop().
    """

    def __init__(self, symbol: str, config: TickLoggerConfig) -> None:
        self._symbol: str = symbol.lower()
        self._batch_size: int = config.batch_size
        self._flush_interval_s: float = config.flush_interval_s
        self._data_dir: Path = Path(config.data_dir)
        self._trade_queue: asyncio.Queue[TradeRecord | None] = asyncio.Queue(maxsize=config.batch_size * 2)
        self._bbo_queue: asyncio.Queue[BBORecord | None] = asyncio.Queue(maxsize=config.batch_size * 2)
        self._worker_tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """Create the data directory and launch both background flush workers."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._worker_tasks = [
            asyncio.create_task(
                self._flush_worker(self._trade_queue, "trades"),
                name="tick_logger_trades",
            ),
            asyncio.create_task(
                self._flush_worker(self._bbo_queue, "bbo"),
                name="tick_logger_bbo",
            ),
        ]
        logger.info(
            "TickLogger started — data_dir=%s batch_size=%d flush_interval=%.1fs",
            self._data_dir,
            self._batch_size,
            self._flush_interval_s,
        )

    async def stop(self) -> None:
        """Signal workers to stop, drain all queues, and flush final batches."""
        await self._trade_queue.put(_SENTINEL)
        await self._bbo_queue.put(_SENTINEL)
        if self._worker_tasks:
            await asyncio.gather(*self._worker_tasks, return_exceptions=True)
            self._worker_tasks.clear()
        logger.info("TickLogger stopped — all queues drained and flushed")

    def put_trade(self, record: TradeRecord) -> None:
        """Enqueue a trade tick for async persistence without blocking the loop.

        Drops the record with a warning if the trade queue is at capacity.
        """
        try:
            self._trade_queue.put_nowait(record)
        except asyncio.QueueFull:
            logger.warning(
                "Trade queue full — dropping record ts_ms=%s",
                record.get("timestamp_ms"),
            )

    def put_bbo(self, record: BBORecord) -> None:
        """Enqueue a BBO snapshot for async persistence without blocking the loop.

        Drops the record with a warning if the BBO queue is at capacity.
        """
        try:
            self._bbo_queue.put_nowait(record)
        except asyncio.QueueFull:
            logger.warning(
                "BBO queue full — dropping record ts_ms=%s",
                record.get("timestamp_ms"),
            )

    async def _flush_worker(
        self,
        queue: asyncio.Queue[dict[str, Any] | None],
        record_type: str,
    ) -> None:
        """Continuously drain a queue and persist batches to Parquet.

        Flushes when either condition is met:
        1. ``len(batch) >= batch_size``
        2. ``flush_interval_s`` seconds have elapsed since the last flush and ``batch`` is non-empty.
        A sentinel value (None) triggers a final flush then clean worker exit.
        """
        batch: list[dict[str, Any]] = []
        last_flush: float = time.monotonic()

        while True:
            time_since_flush: float = time.monotonic() - last_flush
            remaining_timeout: float = max(0.1, self._flush_interval_s - time_since_flush)
            try:
                record = await asyncio.wait_for(queue.get(), timeout=remaining_timeout)
            except TimeoutError:
                if batch:
                    await self._write_parquet(batch, record_type)
                    batch.clear()
                    last_flush = time.monotonic()
                continue

            queue.task_done()

            if record is _SENTINEL:
                if batch:
                    await self._write_parquet(batch, record_type)
                return

            batch.append(record)

            now: float = time.monotonic()
            if len(batch) >= self._batch_size or (now - last_flush >= self._flush_interval_s):
                await self._write_parquet(batch, record_type)
                batch.clear()
                last_flush = now

    async def _write_parquet(
        self,
        batch: list[dict[str, Any]],
        record_type: str,
    ) -> None:
        """Serialise a record batch to a timestamped Parquet file.

        The blocking df.to_parquet call is executed in a thread-pool worker
        via asyncio.to_thread so the event loop remains unblocked during I/O.
        File names encode record type, symbol, and UTC timestamp to millisecond
        precision to prevent collisions between rapid successive flushes.
        """
        df = pd.DataFrame(batch)
        ts_str: str = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")[:-3]
        path: Path = self._data_dir / f"{record_type}_{self._symbol}_{ts_str}.parquet"
        try:
            await asyncio.to_thread(df.to_parquet, path, index=False, engine="pyarrow")
            logger.info(
                "Flushed %d %s records → %s",
                len(df),
                record_type,
                path,
            )
        except Exception as exc:
            logger.error(
                "Parquet write failed for %s (%d records): %s",
                record_type,
                len(df),
                exc,
            )
