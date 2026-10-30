from __future__ import annotations

import abc
from typing import Any

from crypto_quant.execution.interfaces import ExecutionEngine
from crypto_quant.visualizer.cli_dashboard import CLIDashboard


class DataStreamer(abc.ABC):
    """Abstract contract for market data streaming clients."""

    @abc.abstractmethod
    def __init__(
        self,
        config: Any,
        dashboard: CLIDashboard | None = None,
        execution_engine: ExecutionEngine | None = None,
    ) -> None:
        """Initialize the streamer."""

    @abc.abstractmethod
    async def start(self) -> None:
        """Start the streaming connection."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Stop the streaming connection."""
