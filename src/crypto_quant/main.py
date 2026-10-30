"""V3-Crypto-Quant entry point.

Set ``LIVE_MODE=True`` in ``.env`` to route signals through ``CCXTBroker``
(real Hyperliquid orders). The default ``LIVE_MODE=False`` uses ``PaperTrader``.

``DASHBOARD_ENABLED=False`` runs headless (Docker): logs go to stdout and the
Rich UI is skipped. Prometheus metrics are served on ``METRICS_PORT``.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Final

from crypto_quant.config.settings import SETTINGS
from crypto_quant.data_ingestion.hyperliquid_streamer import HyperliquidStreamer
from crypto_quant.execution.ccxt_broker import CCXTBroker
from crypto_quant.execution.interfaces import ExecutionEngine
from crypto_quant.execution.live_engine import PaperTrader
from crypto_quant.observability import metrics
from crypto_quant.observability.logging_setup import configure_logging
from crypto_quant.quant_models.macro_filter import MacroCalendarFilter
from crypto_quant.quant_models.position_sizer import PositionSizer
from crypto_quant.quant_models.sentiment_analyzer import CryptoSentimentAnalyzer
from crypto_quant.visualizer.cli_dashboard import CLIDashboard

_LOG: logging.Logger = logging.getLogger(__name__)
_GEX_FILE: Final[Path] = Path("data/live_gex.json")
_METRICS_SAMPLE_INTERVAL_S: Final[float] = 15.0


def _package_version() -> str:
    try:
        return version("crypto-quant")
    except PackageNotFoundError:
        return "dev"


async def _build_execution_engine() -> ExecutionEngine:
    """Construct and initialise the execution engine selected by ``LIVE_MODE``."""
    position_sizer = PositionSizer()
    strategy = SETTINGS.strategy
    if SETTINGS.live_mode:
        broker_cfg = SETTINGS.build_broker()
        engine = CCXTBroker(
            exchange_id="hyperliquid",
            api_key=broker_cfg.api_key,
            api_secret=broker_cfg.api_secret,
            testnet=broker_cfg.testnet,
            # Hyperliquid perps are USDC-margined: CCXT unified symbol is BASE/USDC:USDC
            symbol=f"{SETTINGS.hyperliquid_ws.symbol}/USDC:USDC",
            max_order_size_btc=SETTINGS.max_order_size_btc,
            default_leverage=SETTINGS.default_leverage,
            slippage_tolerance_pct=SETTINGS.slippage_tolerance_pct,
            position_sizer=position_sizer,
            sl_pct=strategy.sl_pct,
            tp_pct=strategy.tp_pct,
            fee_rate=strategy.taker_fee_rate,
        )
        await engine.initialize()
        _LOG.critical(
            "=== LIVE MODE ACTIVE — real orders will be submitted to Hyperliquid (testnet=%s) ===",
            broker_cfg.testnet,
        )
        return engine

    _LOG.info("Paper-trading mode active — no real orders will be submitted.")
    return PaperTrader(
        position_sizer=position_sizer,
        sl_pct=strategy.sl_pct,
        tp_pct=strategy.tp_pct,
        fee_rate=strategy.taker_fee_rate,
        slippage_bps=strategy.slippage_bps,
        max_order_size_btc=SETTINGS.max_order_size_btc,
        max_leverage=float(SETTINGS.default_leverage),
    )


def _register_sigterm(loop: asyncio.AbstractEventLoop, main_task: asyncio.Task) -> None:
    """Cancel the main task on SIGTERM for clean container/systemd shutdown."""

    def _on_sigterm() -> None:
        _LOG.warning("SIGTERM received — initiating graceful shutdown.")
        main_task.cancel()

    try:
        loop.add_signal_handler(signal.SIGTERM, _on_sigterm)
    except NotImplementedError:
        pass


async def _update_background_tasks(
    dashboard: CLIDashboard | None,
    macro_filter: MacroCalendarFilter,
    sentiment_analyzer: CryptoSentimentAnalyzer,
) -> None:
    """Periodically fetch macro and sentiment data and update the dashboard."""
    while True:
        try:
            # Re-fetch macro calendar if UTC day has changed or initial fetch missing
            now_utc = datetime.now(UTC)
            if macro_filter.last_fetch is None or now_utc.date() != macro_filter.last_fetch.date():
                _LOG.info("Refreshing macro calendar for UTC date %s", now_utc.date())
                await macro_filter.fetch_events()

            # Update sentiment (runs FinBERT in thread pool)
            score = await sentiment_analyzer.update_sentiment()
            metrics.SENTIMENT_NET.set(score.net_score)

            allowed = macro_filter.is_trading_allowed()
            metrics.MACRO_BLACKOUT.set(0 if allowed else 1)
            macro_status = "SAFE (Trading Allowed)" if allowed else "DANGER (Blackout)"

            sentiment_str = (
                f"Bull: {score.bullish_mentions} | Bear: {score.bearish_mentions} | Net: {score.net_score:.2f}"
            )

            if dashboard is not None:
                dashboard.update_extras(sentiment=sentiment_str, macro=macro_status)

            # Wait 5 minutes before updating sentiment again
            await asyncio.sleep(300)
        except asyncio.CancelledError:
            break
        except Exception as e:
            _LOG.error("Background task error: %s", e)
            await asyncio.sleep(60)


def _gex_snapshot_age_s(path: Path = _GEX_FILE, now_s: float | None = None) -> float:
    """Seconds since the GEX collector last rewrote ``live_gex.json``; -1 if missing."""
    try:
        mtime: float = path.stat().st_mtime
    except OSError:
        return -1.0
    return (now_s if now_s is not None else time.time()) - mtime


async def _sample_runtime_metrics(engine: ExecutionEngine) -> None:
    """Publish engine risk state and GEX freshness to Prometheus gauges."""
    while True:
        try:
            balance: float = await engine.fetch_balance()
            metrics.record_engine_snapshot(engine.risk_snapshot(), balance)
            metrics.GEX_SNAPSHOT_AGE.set(_gex_snapshot_age_s())
            await asyncio.sleep(_METRICS_SAMPLE_INTERVAL_S)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            _LOG.error("Metrics sampler error: %s", exc)
            await asyncio.sleep(_METRICS_SAMPLE_INTERVAL_S)


async def _run() -> None:
    macro_filter = MacroCalendarFilter()
    await macro_filter.fetch_events()

    sentiment_analyzer = CryptoSentimentAnalyzer()

    engine: ExecutionEngine = await _build_execution_engine()
    dashboard: CLIDashboard | None = (
        CLIDashboard(symbol=SETTINGS.hyperliquid_ws.symbol) if SETTINGS.dashboard_enabled else None
    )
    streamer = HyperliquidStreamer(
        config=SETTINGS,
        dashboard=dashboard,
        execution_engine=engine,
        macro_filter=macro_filter,
        sentiment_analyzer=sentiment_analyzer,
    )

    bg_tasks = [
        asyncio.create_task(_update_background_tasks(dashboard, macro_filter, sentiment_analyzer)),
        asyncio.create_task(_sample_runtime_metrics(engine)),
    ]

    try:
        await streamer.start()
    finally:
        for task in bg_tasks:
            task.cancel()
        await streamer.stop()
        if isinstance(engine, CCXTBroker):
            await engine.close()
        _LOG.info("V3-Crypto-Quant shutdown complete.")


def main() -> None:
    configure_logging(
        level=SETTINGS.log_level,
        log_file=SETTINGS.log_file,
        log_format=SETTINGS.log_format,
        to_stdout=not SETTINGS.dashboard_enabled,
    )
    metrics.start_metrics_server(
        SETTINGS.metrics_port,
        build_labels={
            "version": _package_version(),
            "mode": "live" if SETTINGS.live_mode else "paper",
            "symbol": SETTINGS.hyperliquid_ws.symbol,
        },
    )
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
