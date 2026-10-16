"""V3-Crypto-Quant entry point.

Set ``LIVE_MODE=True`` in ``.env`` to route signals through ``CCXTBroker``
(real Bybit orders). The default ``LIVE_MODE=False`` uses ``PaperTrader``.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from config.settings import SETTINGS
from data_ingestion.hyperliquid_streamer import HyperliquidStreamer
from execution.ccxt_broker import CCXTBroker
from execution.interfaces import ExecutionEngine
from execution.live_engine import PaperTrader
from quant_models.macro_filter import MacroCalendarFilter
from visualizer.cli_dashboard import CLIDashboard

_LOG: logging.Logger = logging.getLogger(__name__)


def _configure_logging(
    level: str = "INFO",
    log_file: str = "logs/app.log",
    retention_days: int = 5,
) -> None:
    """Route all output to a rotating file so Rich owns stdout exclusively.

    Rotates daily at midnight and retains log files for the last ``retention_days``.
    """
    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        filename=log_file,
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding="utf-8",
    )
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
            max_order_size_btc=SETTINGS.max_order_size_btc,
            default_leverage=SETTINGS.default_leverage,
            slippage_tolerance_pct=SETTINGS.slippage_tolerance_pct,
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


async def _update_background_tasks(dashboard: CLIDashboard, macro_filter: MacroCalendarFilter) -> None:
    """Periodically fetch macro and sentiment data and update the dashboard."""
    from quant_models.sentiment_analyzer import CryptoSentimentAnalyzer

    sentiment_analyzer = CryptoSentimentAnalyzer()

    while True:
        try:
            # Update sentiment (takes a few seconds)
            score = await sentiment_analyzer.update_sentiment()

            allowed = macro_filter.is_trading_allowed()
            macro_status = "SAFE (Trading Allowed)" if allowed else "DANGER (Blackout)"

            sentiment_str = (
                f"Bull: {score.bullish_mentions} | Bear: {score.bearish_mentions} | Net: {score.net_score:.2f}"
            )

            dashboard.update_extras(sentiment=sentiment_str, macro=macro_status)

            # Wait 5 minutes before updating sentiment again
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            break
        except Exception as e:
            _LOG.error("Background task error: %s", e)
            await asyncio.sleep(60)


async def _run() -> None:
    macro_filter = MacroCalendarFilter()
    await macro_filter.fetch_events()

    engine: ExecutionEngine = await _build_execution_engine()
    dashboard = CLIDashboard(symbol=SETTINGS.hyperliquid_ws.symbol)
    streamer = HyperliquidStreamer(
        config=SETTINGS,
        dashboard=dashboard,
        execution_engine=engine,
        macro_filter=macro_filter,
    )

    # Start the background task
    bg_task = asyncio.create_task(_update_background_tasks(dashboard, macro_filter))

    try:
        await streamer.start()
    finally:
        bg_task.cancel()
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
