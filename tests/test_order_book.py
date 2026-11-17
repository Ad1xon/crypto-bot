"""Tests for data_ingestion.order_book.

The same tests run against the Python fallback and, if it's installed, the C++
crypto_core.OrderBook, so the two can't drift apart.
"""

from __future__ import annotations

import logging
import math
import sys
from collections.abc import Callable
from typing import Any, Final

import pytest

from crypto_quant.data_ingestion.order_book import (
    OrderBookLike,
    PyOrderBook,
    create_order_book,
)

DAY_MS: Final[int] = 86_400_000
T0: Final[int] = 1_763_251_200_000  # 2025-11-16 00:00:00 UTC

BookFactory = Callable[..., OrderBookLike]


def _cpp_factory(**kwargs: Any) -> OrderBookLike:
    crypto_core = pytest.importorskip("crypto_core")
    book: OrderBookLike = crypto_core.OrderBook(**kwargs)
    return book


@pytest.fixture(params=["python", "crypto_core"])
def make_book(request: pytest.FixtureRequest) -> BookFactory:
    if request.param == "python":
        return PyOrderBook
    return _cpp_factory


def _side(rows: list[tuple[float, float]]) -> list[dict[str, Any]]:
    return [{"px": str(px), "sz": str(sz), "n": 1} for px, sz in rows]


def _levels(bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> list[list[dict[str, Any]]]:
    return [_side(bids), _side(asks)]


def test_bbo_from_hyperliquid_levels(make_book: BookFactory) -> None:
    book = make_book()
    book.update_hyperliquid(_levels([(100.0, 2.0), (99.0, 3.0)], [(101.0, 1.0), (102.0, 4.0)]), T0)
    assert book.best_bid_price == 100.0
    assert book.best_bid_size == 2.0
    assert book.best_ask_price == 101.0
    assert book.best_ask_size == 1.0
    assert book.mid_price == pytest.approx(100.5)
    assert book.spread == pytest.approx(1.0)
    assert book.top_imbalance == pytest.approx(1.0)
    assert [tuple(row) for row in book.bids()] == [(100.0, 2.0), (99.0, 3.0)]


def test_one_sided_book(make_book: BookFactory) -> None:
    book = make_book()
    book.update_hyperliquid(_levels([(100.0, 2.0)], []), T0)
    assert not book.is_two_sided
    assert book.mid_price == 0.0
    assert book.spread == 0.0
    assert book.best_ask_price == 0.0


def test_depth_and_invalid_rows(make_book: BookFactory) -> None:
    # Only the first `depth` rows are read, invalid ones among them are dropped.
    book = make_book(depth=2)
    bids = [(100.0, 0.0), (99.5, 1.0), (99.0, 2.0), (98.5, 3.0)]
    book.update_hyperliquid(_levels(bids, [(101.0, 1.0)]), T0)
    assert [tuple(row) for row in book.bids()] == [(99.5, 1.0)]


def test_missing_px_raises_key_error(make_book: BookFactory) -> None:
    book = make_book()
    with pytest.raises(KeyError):
        book.update_hyperliquid([[{"sz": "1.0"}], []], T0)


def test_cvd_and_daily_reset(make_book: BookFactory) -> None:
    book = make_book()
    book.apply_trade(100.0, 3.0, True, T0 + 1_000)
    book.apply_trade(100.0, 1.0, False, T0 + DAY_MS - 1)
    assert book.cvd == pytest.approx(2.0)

    book.apply_trade(100.0, 0.5, True, T0 + DAY_MS + 5)
    assert book.cvd == pytest.approx(0.5)

    # A late trade from the previous day must not reset CVD a second time.
    book.apply_trade(100.0, 0.25, True, T0 + DAY_MS - 10)
    book.apply_trade(100.0, 0.25, True, T0 + DAY_MS + 10)
    assert book.cvd == pytest.approx(1.0)


def test_cvd_ignores_bad_sizes_and_can_be_reset(make_book: BookFactory) -> None:
    book = make_book(auto_reset_cvd=False)
    book.apply_trade(100.0, math.nan, True, T0)
    book.apply_trade(100.0, -1.0, True, T0)
    assert book.cvd == 0.0

    book.apply_trade(100.0, 1.0, True, T0)
    book.apply_trade(100.0, 1.0, True, T0 + 3 * DAY_MS)
    assert book.cvd == pytest.approx(2.0)

    book.reset_cvd(5.0)
    assert book.cvd == 5.0


def test_python_book_rejects_bad_depth() -> None:
    with pytest.raises(ValueError, match="depth must be in"):
        PyOrderBook(depth=0)


def test_create_order_book_uses_cpp_when_installed() -> None:
    crypto_core = pytest.importorskip("crypto_core")
    assert isinstance(create_order_book(), crypto_core.OrderBook)


def test_create_order_book_falls_back_to_python(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Setting the module to None makes `import crypto_core` raise ImportError.
    monkeypatch.setitem(sys.modules, "crypto_core", None)
    with caplog.at_level(logging.WARNING):
        fallback = create_order_book(auto_reset_cvd=False)
    assert isinstance(fallback, PyOrderBook)
    assert "not installed" in caplog.text
