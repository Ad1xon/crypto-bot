"""V3-Crypto-Quant entry point.

Set ``LIVE_MODE=True`` in ``.env`` to route signals through ``CCXTBroker``
(real Bybit orders). The default ``LIVE_MODE=False`` uses ``PaperTrader``.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from config.settings import SETTINGS
from data_ingestion.hyperliquid_streamer import HyperliquidStreamer
from execution.ccxt_broker import CCXTBroker
from execution.interfaces import ExecutionEngine
from execution.live_engine import PaperTrader
from visualizer.cli_dashboard import CLIDashboard

_LOG: logging.Logger = logging.getLogger(__name__)


def _configure_logging(level: str = "INFO", log_file: str = "app.log") -> None:
    """Route all output to a file so Rich owns stdout exclusively."""
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[file_handler],
    )


async def _build_execution_engine() -> ExecutionEngine:
    """Construct and initialise the execution engine selected by ``LIVE_MODE``."""
    if SETTINGS.live_mode:
        broker_cfg = SETTINGS.build_broker()
        engine = CCXTBroker(
            exchange_id="hyperliquid",
            api_key=broker_cfg.api_key,
            api_secret=broker_cfg.api_secret,
            testnet=broker_cfg.testnet,
        )
        await engine.initialize()
        _LOG.critical(
            "=== LIVE MODE ACTIVE — real orders will be submitted to Hyperliquid (testnet=%s) ===",
            broker_cfg.testnet,
        )
        return engine

    _LOG.info("Paper-trading mode active — no real orders will be submitted.")
    return PaperTrader()


def _register_sigterm(loop: asyncio.AbstractEventLoop, main_task: asyncio.Task) -> None:
    """Cancel the main task on SIGTERM for clean container/systemd shutdown."""

    def _on_sigterm() -> None:
        _LOG.warning("SIGTERM received — initiating graceful shutdown.")
        main_task.cancel()

    try:
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    except NotImplementedError:
        pass


async def _run() -> None:
    engine: ExecutionEngine = await _build_execution_engine()
    dashboard = CLIDashboard(symbol=SETTINGS.hyperliquid_ws.symbol)
    streamer = HyperliquidStreamer(
        config=SETTINGS,
        dashboard=dashboard,
        execution_engine=engine,
    )
    try:
        await streamer.start()
    finally:
        await streamer.stop()
        if isinstance(engine, CCXTBroker):
            await engine.close()
        _LOG.info("V3-Crypto-Quant shutdown complete.")


def main() -> None:
    _configure_logging(level=SETTINGS.log_level, log_file=SETTINGS.log_file)
    _LOG.info(
        "V3-Crypto-Quant starting — symbol=%s live_mode=%s log_file=%s",
        SETTINGS.hyperliquid_ws.symbol,
        SETTINGS.live_mode,
        SETTINGS.log_file,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    main_task: asyncio.Task = loop.create_task(_run())
    _register_sigterm(loop, main_task)

    try:
        loop.run_until_complete(main_task)
    except KeyboardInterrupt:
        _LOG.info("Shutdown requested via KeyboardInterrupt — cancelling tasks.")
        main_task.cancel()
        loop.run_until_complete(asyncio.gather(main_task, return_exceptions=True))
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()
        _LOG.info("Event loop closed — exiting.")
    sys.exit(0)


if __name__ == "__main__":
    main()
