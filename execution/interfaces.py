from __future__ import annotations

import abc


class ExecutionEngine(abc.ABC):
    """Abstract contract for all execution back-ends (paper, live, backtest)."""

    @property
    @abc.abstractmethod
    def has_open_position(self) -> bool: ...

    @abc.abstractmethod
    def is_on_cooldown(self, current_time: float) -> bool: ...

    @abc.abstractmethod
    async def fetch_balance(self) -> float: ...

    @abc.abstractmethod
    async def update_tick(self, current_price: float, current_time: float) -> None: ...

    @abc.abstractmethod
    async def execute_signal(
        self,
        signal_type: str,
        current_price: float,
        current_time: float,
        gex_value: float = 0.0,
        signal_ts: float | None = None,
    ) -> None: ...
