from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_exchange() -> MagicMock:
    """Fully mocked ccxt async exchange with AsyncMocks for all coroutine methods."""
    ex = MagicMock()
    ex.sandbox = False
    ex.set_sandbox_mode = MagicMock()
    ex.fetch_balance = AsyncMock(return_value={"free": {"USDT": 10_000.0}})
    ex.amount_to_precision = MagicMock(side_effect=lambda sym, amt: str(round(float(amt), 6)))
    ex.price_to_precision = MagicMock(side_effect=lambda sym, price: str(round(float(price), 2)))
    ex.create_market_buy_order = AsyncMock(return_value={"id": "mock_buy_001"})
    ex.create_market_sell_order = AsyncMock(return_value={"id": "mock_sell_001"})
    ex.cancel_all_orders = AsyncMock()
    return ex
