"""Central configuration for V3-Crypto-Quant.

Loads overrides from a .env file via python-dotenv.  Broker credentials are
mandatory — a descriptive RuntimeError is raised at startup if any required
variable is absent so the system never silently reaches live order execution
without proper configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _require_env(key: str) -> str:
    value: str = os.getenv(key, "")
    if not value or value == "your_api_key_here" or value == "your_api_secret_here":
        raise RuntimeError(f"{key} is missing in .env — set a real value before starting the live broker.")
    return value


@dataclass(frozen=True)
class BrokerConfig:
    """Immutable broker credentials (now mapped for Hyperliquid) and sandbox toggle."""

    api_key: str = field(default_factory=lambda: _require_env("HYPERLIQUID_WALLET_ADDRESS"))
    api_secret: str = field(default_factory=lambda: _require_env("HYPERLIQUID_PRIVATE_KEY"))
    testnet: bool = field(
        default_factory=lambda: os.getenv("USE_TESTNET", "True").strip().lower() in {"true", "1", "yes"}
    )


@dataclass(frozen=True)
class DeribitConfig:
    """Immutable Deribit REST API parameters."""

    base_url: str = field(default_factory=lambda: os.getenv("DERIBIT_BASE_URL", "https://www.deribit.com/api/v2"))


@dataclass(frozen=True)
class HyperliquidWSConfig:
    """Immutable Hyperliquid WebSocket connection parameters."""

    ws_url: str = field(
        default_factory=lambda: (
            "wss://api.hyperliquid-testnet.xyz/ws"
            if os.getenv("USE_TESTNET", "True").strip().lower() in {"true", "1", "yes"}
            else "wss://api.hyperliquid.xyz/ws"
        )
    )
    symbol: str = field(default_factory=lambda: os.getenv("TRADING_SYMBOL", "BTC").upper())


@dataclass(frozen=True)
class ReconnectPolicy:
    """Exponential back-off parameters for WebSocket reconnection."""

    initial_delay_s: float = 1.0
    max_delay_s: float = 60.0
    backoff_factor: float = 2.0
    max_attempts: int = 10


@dataclass(frozen=True)
class TickLoggerConfig:
    """Configuration for the non-blocking asynchronous tick-data logger."""

    batch_size: int = field(default_factory=lambda: int(os.getenv("TICK_BATCH_SIZE", "5000")))
    flush_interval_s: float = field(default_factory=lambda: float(os.getenv("TICK_FLUSH_INTERVAL_S", "30.0")))
    data_dir: str = field(default_factory=lambda: os.getenv("TICK_DATA_DIR", "data"))


@dataclass(frozen=True)
class AlertingConfig:
    """Discord webhook alerting configuration for critical system events."""

    discord_webhook_url: str = field(default_factory=lambda: os.getenv("DISCORD_WEBHOOK_URL", ""))


@dataclass(frozen=True)
class AppConfig:
    """Top-level application configuration composed of sub-configs."""

    hyperliquid_ws: HyperliquidWSConfig = field(default_factory=HyperliquidWSConfig)
    reconnect: ReconnectPolicy = field(default_factory=ReconnectPolicy)
    tick_logger: TickLoggerConfig = field(default_factory=TickLoggerConfig)
    deribit: DeribitConfig = field(default_factory=DeribitConfig)
    alerting: AlertingConfig = field(default_factory=AlertingConfig)
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    log_file: str = field(default_factory=lambda: os.getenv("LOG_FILE", "logs/app.log"))
    live_mode: bool = field(
        default_factory=lambda: os.getenv("LIVE_MODE", "False").strip().lower() in {"true", "1", "yes"}
    )

    def build_broker(self) -> BrokerConfig:
        """Construct and validate BrokerConfig.

        Deferred so that processes that never touch the live broker (e.g. the
        paper-trading loop) are not forced to supply credentials.  Raises a
        descriptive RuntimeError immediately when a required variable is absent
        or still holds its placeholder value.
        """
        return BrokerConfig()


SETTINGS: AppConfig = AppConfig()
