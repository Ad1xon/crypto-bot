"""Order book used by HyperliquidStreamer.

Uses the C++ ``crypto_core.OrderBook`` (src/cpp_core) when the extension is
installed. Otherwise falls back to ``PyOrderBook``, which implements the part of
the interface the streamer needs in pure Python (e.g. on a dev machine without a
C++ compiler). Both return the same BBO, ``top_imbalance`` and CVD for the same
input, so signals don't depend on which one is active.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Iterable, Sequence
from typing import Any, Final, Protocol

logger: logging.Logger = logging.getLogger(__name__)

MAX_DEPTH: Final[int] = 20
_MS_PER_DAY: Final[int] = 86_400_000

# Hyperliquid l2Book payload: [bids, asks], each a list of {"px": str, "sz": str, "n": int}.
HyperliquidLevels = Sequence[Sequence[dict[str, Any]]]


class OrderBookLike(Protocol):
    def update_hyperliquid(self, levels: HyperliquidLevels, timestamp_ms: int = 0) -> float: ...
    def apply_trade(self, price: float, size: float, is_buy: bool, timestamp_ms: int = 0) -> float: ...
    def reset_cvd(self, value: float = 0.0) -> None: ...
    def bids(self) -> Iterable[Sequence[float]]: ...
    def asks(self) -> Iterable[Sequence[float]]: ...
    @property
    def best_bid_price(self) -> float: ...
    @property
    def best_bid_size(self) -> float: ...
    @property
    def best_ask_price(self) -> float: ...
    @property
    def best_ask_size(self) -> float: ...
    @property
    def is_two_sided(self) -> bool: ...
    @property
    def mid_price(self) -> float: ...
    @property
    def spread(self) -> float: ...
    @property
    def top_imbalance(self) -> float: ...
    @property
    def cvd(self) -> float: ...


class PyOrderBook:
    """Pure-Python fallback. Doesn't compute OFI, only what the streamer uses."""

    def __init__(self, depth: int = MAX_DEPTH, auto_reset_cvd: bool = True) -> None:
        if not 1 <= depth <= MAX_DEPTH:
            raise ValueError(f"depth must be in [1, {MAX_DEPTH}], got {depth}")
        self._depth = depth
        self._auto_reset_cvd = auto_reset_cvd
        self._bids: list[tuple[float, float]] = []
        self._asks: list[tuple[float, float]] = []
        self._cvd = 0.0
        self._last_trade_day = -1

    def _load_side(self, side: Sequence[dict[str, Any]]) -> list[tuple[float, float]]:
        rows: list[tuple[float, float]] = []
        # Same as the C++ book: take the first `depth` rows, then drop the invalid ones.
        for level in side[: self._depth]:
            px, sz = float(level["px"]), float(level["sz"])
            if math.isfinite(px) and math.isfinite(sz) and sz > 0.0:
                rows.append((px, sz))
        return rows

    def update_hyperliquid(self, levels: HyperliquidLevels, timestamp_ms: int = 0) -> float:
        """Replace the book with a new snapshot. Returns 0.0 because OFI isn't computed here."""
        self._bids = self._load_side(levels[0]) if len(levels) > 0 else []
        self._asks = self._load_side(levels[1]) if len(levels) > 1 else []
        return 0.0

    def apply_trade(self, price: float, size: float, is_buy: bool, timestamp_ms: int = 0) -> float:
        if not math.isfinite(size) or size < 0.0:
            return self._cvd
        if self._auto_reset_cvd and timestamp_ms > 0:
            day = timestamp_ms // _MS_PER_DAY
            # The day only moves forward, so a late trade from yesterday can't reset CVD twice.
            if day > self._last_trade_day:
                if self._last_trade_day >= 0:
                    self._cvd = 0.0
                self._last_trade_day = day
        self._cvd += size if is_buy else -size
        return self._cvd

    def reset_cvd(self, value: float = 0.0) -> None:
        self._cvd = value

    def bids(self) -> list[tuple[float, float]]:
        return list(self._bids)

    def asks(self) -> list[tuple[float, float]]:
        return list(self._asks)

    @property
    def best_bid_price(self) -> float:
        return self._bids[0][0] if self._bids else 0.0

    @property
    def best_bid_size(self) -> float:
        return self._bids[0][1] if self._bids else 0.0

    @property
    def best_ask_price(self) -> float:
        return self._asks[0][0] if self._asks else 0.0

    @property
    def best_ask_size(self) -> float:
        return self._asks[0][1] if self._asks else 0.0

    @property
    def is_two_sided(self) -> bool:
        return bool(self._bids) and bool(self._asks)

    @property
    def mid_price(self) -> float:
        return (self.best_bid_price + self.best_ask_price) / 2.0 if self.is_two_sided else 0.0

    @property
    def spread(self) -> float:
        return self.best_ask_price - self.best_bid_price if self.is_two_sided else 0.0

    @property
    def top_imbalance(self) -> float:
        return self.best_bid_size - self.best_ask_size

    @property
    def cvd(self) -> float:
        return self._cvd


def create_order_book(depth: int = MAX_DEPTH, auto_reset_cvd: bool = True) -> OrderBookLike:
    """Return the C++ order book if it's installed, otherwise the Python fallback."""
    try:
        import crypto_core
    except ImportError:
        logger.warning("crypto_core C++ extension not installed, using the slower Python order book")
        return PyOrderBook(depth=depth, auto_reset_cvd=auto_reset_cvd)
    logger.info("Using crypto_core %s C++ order book", crypto_core.__version__)
    book: OrderBookLike = crypto_core.OrderBook(depth=depth, auto_reset_cvd=auto_reset_cvd)
    return book
