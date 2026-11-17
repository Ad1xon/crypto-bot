"""Tests for the crypto_core C++ OrderBook (src/cpp_core).

The extension has to be compiled first (see docs/cpp_core.md). If it isn't
installed the whole module is skipped; CI builds it and imports it before
pytest runs, so a broken build still fails the pipeline there.
"""

from __future__ import annotations

import math
from typing import Final

import numpy as np
import pytest

crypto_core = pytest.importorskip("crypto_core")

DAY_MS: Final[int] = 86_400_000
T0: Final[int] = 1_763_251_200_000  # 2025-11-16 00:00:00 UTC

BIDS: Final[list[list[float]]] = [[100.0, 2.0], [99.0, 3.0]]
ASKS: Final[list[list[float]]] = [[101.0, 1.0], [102.0, 4.0]]


def _arr(rows: list[list[float]]) -> np.ndarray:
    return np.asarray(rows, dtype=np.float64).reshape(-1, 2)


def _hl_side(rows: list[list[float]]) -> list[dict[str, str | int]]:
    """Format rows the way Hyperliquid's l2Book message does (strings for px/sz)."""
    return [{"px": str(px), "sz": str(sz), "n": 1} for px, sz in rows]


@pytest.fixture
def book() -> crypto_core.OrderBook:
    ob = crypto_core.OrderBook()
    ob.update_book(_arr(BIDS), _arr(ASKS), T0)
    return ob


# --------------------------------------------------------------------- book / BBO


def test_bbo_mid_price_and_spread(book: crypto_core.OrderBook) -> None:
    assert book.best_bid_price == 100.0
    assert book.best_ask_price == 101.0
    assert book.best_bid_size == 2.0
    assert book.best_ask_size == 1.0
    assert book.mid_price == pytest.approx(100.5)
    assert book.spread == pytest.approx(1.0)
    assert book.is_two_sided
    assert book.bid_levels == 2
    assert book.ask_levels == 2
    assert book.last_update_ms == T0


def test_microprice_leans_towards_the_thinner_side(book: crypto_core.OrderBook) -> None:
    # (100 * 1 + 101 * 2) / (2 + 1): more size on the bid pushes it towards the ask.
    assert book.microprice == pytest.approx(302.0 / 3.0)
    assert book.top_imbalance == pytest.approx(1.0)


def test_depth_imbalance(book: crypto_core.OrderBook) -> None:
    assert book.depth_imbalance(1) == pytest.approx((2.0 - 1.0) / 3.0)
    assert book.depth_imbalance(2) == pytest.approx((5.0 - 5.0) / 10.0)


def test_one_sided_book_returns_zero_for_mid_and_spread() -> None:
    ob = crypto_core.OrderBook()
    ob.update_book(_arr(BIDS), _arr([]), T0)
    assert not ob.is_two_sided
    assert ob.mid_price == 0.0
    assert ob.spread == 0.0
    assert ob.microprice == 0.0


def test_invalid_rows_are_skipped_without_shifting_levels() -> None:
    ob = crypto_core.OrderBook()
    bids = _arr([[math.nan, 1.0], [100.0, 0.0], [99.5, 2.0], [99.0, math.inf]])
    ob.update_book(bids, _arr(ASKS), T0)
    assert ob.bid_levels == 1
    assert ob.best_bid_price == 99.5
    np.testing.assert_allclose(ob.bids(), [[99.5, 2.0]])


def test_rows_beyond_depth_are_ignored() -> None:
    ob = crypto_core.OrderBook(depth=1)
    ob.update_book(_arr(BIDS), _arr(ASKS), T0)
    assert ob.bid_levels == 1
    assert ob.ask_levels == 1
    assert len(ob.ofi_levels()) == 1


def test_snapshot_dict_matches_properties(book: crypto_core.OrderBook) -> None:
    snap = book.snapshot()
    assert snap["mid_price"] == pytest.approx(book.mid_price)
    assert snap["spread"] == pytest.approx(book.spread)
    assert snap["best_bid_qty"] == book.best_bid_size
    assert snap["best_ask_qty"] == book.best_ask_size
    assert snap["timestamp_ms"] == T0


# ---------------------------------------------------------------------------- OFI


def test_first_snapshot_has_zero_ofi() -> None:
    ob = crypto_core.OrderBook()
    assert ob.update_book(_arr(BIDS), _arr(ASKS), T0) == 0.0
    assert ob.ofi == 0.0
    assert ob.cumulative_ofi == 0.0


@pytest.mark.parametrize(
    ("new_bids", "new_asks", "expected_ofi"),
    [
        # Same prices, bid size 2 -> 5: +5 - 2 (bid) and -1 + 1 (ask).
        ([[100.0, 5.0], [99.0, 3.0]], ASKS, 3.0),
        # Bid improves to 100.5: only the new bid size counts (+1).
        ([[100.5, 1.0], [100.0, 2.0]], ASKS, 1.0),
        # Ask improves to 100.8 with size 3: bid unchanged (0), new ask size counts (-3).
        (BIDS, [[100.8, 3.0], [101.0, 1.0]], -3.0),
        # Best bid pulled down to 99.5: only the old bid size counts (-2).
        ([[99.5, 4.0], [99.0, 3.0]], ASKS, -2.0),
        # Ask pulled up to 101.5: the old ask size counts (+1).
        (BIDS, [[101.5, 2.0], [102.0, 4.0]], 1.0),
    ],
)
def test_level1_ofi(
    book: crypto_core.OrderBook,
    new_bids: list[list[float]],
    new_asks: list[list[float]],
    expected_ofi: float,
) -> None:
    returned = book.update_book(_arr(new_bids), _arr(new_asks), T0 + 100)
    assert returned == pytest.approx(expected_ofi)
    assert book.ofi == pytest.approx(expected_ofi)
    assert book.cumulative_ofi == pytest.approx(expected_ofi)


def test_multi_level_ofi_and_cumulative_ofi(book: crypto_core.OrderBook) -> None:
    # Level 1: bid size 2 -> 4 at the same price = +2.
    # Level 2: ask size 4 -> 1 at the same price = -1 (new) + 4 (old) = +3.
    book.update_book(_arr([[100.0, 4.0], [99.0, 3.0]]), _arr([[101.0, 1.0], [102.0, 1.0]]), T0 + 100)
    np.testing.assert_allclose(book.ofi_levels()[:2], [2.0, 3.0])
    assert book.multi_level_ofi(1) == pytest.approx(2.0)
    assert book.multi_level_ofi(2) == pytest.approx(5.0)

    # Back to the original book: level 1 bid 4 -> 2 gives -2, so the running sum is 0.
    book.update_book(_arr(BIDS), _arr(ASKS), T0 + 200)
    assert book.ofi == pytest.approx(-2.0)
    assert book.cumulative_ofi == pytest.approx(0.0)
    assert book.update_count == 3


def test_vanishing_level_counts_its_full_size(book: crypto_core.OrderBook) -> None:
    # Level 2 bid (99 x 3) disappears -> treated as price 0, so -3 at level 2.
    book.update_book(_arr([[100.0, 2.0]]), _arr(ASKS), T0 + 100)
    assert book.ofi_levels()[1] == pytest.approx(-3.0)


def test_update_hyperliquid_matches_update_book() -> None:
    arrays = crypto_core.OrderBook()
    raw = crypto_core.OrderBook()
    steps = [
        (BIDS, ASKS),
        ([[100.0, 5.0], [99.0, 3.0]], ASKS),
        (BIDS, [[100.8, 3.0], [101.0, 1.0]]),
    ]
    for i, (bids, asks) in enumerate(steps):
        ofi_a = arrays.update_book(_arr(bids), _arr(asks), T0 + i)
        ofi_b = raw.update_hyperliquid([_hl_side(bids), _hl_side(asks)], T0 + i)
        assert ofi_a == pytest.approx(ofi_b)
    assert raw.mid_price == pytest.approx(arrays.mid_price)
    assert raw.spread == pytest.approx(arrays.spread)
    assert raw.cumulative_ofi == pytest.approx(arrays.cumulative_ofi)


# ---------------------------------------------------------------------------- CVD


def test_cvd_from_single_trades() -> None:
    ob = crypto_core.OrderBook()
    assert ob.apply_trade(100.0, 1.5, True, T0) == pytest.approx(1.5)
    assert ob.apply_trade(100.5, 0.5, False, T0 + 1) == pytest.approx(1.0)
    assert ob.cvd == pytest.approx(1.0)
    assert ob.buy_volume == pytest.approx(1.5)
    assert ob.sell_volume == pytest.approx(0.5)
    assert ob.last_trade_price == 100.5
    assert ob.trade_count == 2


def test_cvd_ignores_bad_sizes() -> None:
    ob = crypto_core.OrderBook()
    ob.apply_trade(100.0, math.nan, True, T0)
    ob.apply_trade(100.0, -1.0, True, T0)
    assert ob.cvd == 0.0
    assert ob.trade_count == 0


def test_apply_trades_batch_matches_single_trades() -> None:
    prices = np.array([100.0, 100.1, 100.2, 100.3])
    sizes = np.array([1.0, 2.0, 0.5, 0.25])
    is_buy = np.array([True, False, True, True])
    ts = np.array([T0, T0 + 1, T0 + 2, T0 + 3], dtype=np.int64)

    batch = crypto_core.OrderBook()
    single = crypto_core.OrderBook()
    cvd = batch.apply_trades(prices, sizes, is_buy, ts)
    for p, s, b, t in zip(prices, sizes, is_buy, ts, strict=True):
        single.apply_trade(float(p), float(s), bool(b), int(t))

    assert cvd == pytest.approx(1.0 - 2.0 + 0.5 + 0.25)
    assert batch.cvd == pytest.approx(single.cvd)


def test_apply_trades_rejects_length_mismatch() -> None:
    ob = crypto_core.OrderBook()
    with pytest.raises(ValueError, match="equal length"):
        ob.apply_trades(np.array([1.0]), np.array([1.0, 2.0]), np.array([True]), np.array([T0]))


def test_cvd_resets_on_new_utc_day_only_once() -> None:
    ob = crypto_core.OrderBook()
    ob.apply_trade(100.0, 3.0, True, T0 + 1_000)
    ob.apply_trade(100.0, 1.0, False, T0 + DAY_MS - 1)
    assert ob.cvd == pytest.approx(2.0)

    ob.apply_trade(100.0, 0.5, True, T0 + DAY_MS + 5)
    assert ob.cvd == pytest.approx(0.5)
    assert ob.buy_volume == pytest.approx(0.5)
    assert ob.sell_volume == 0.0

    # A late trade from yesterday must not trigger a second reset.
    ob.apply_trade(100.0, 0.25, True, T0 + DAY_MS - 10)
    assert ob.cvd == pytest.approx(0.75)


def test_cvd_auto_reset_can_be_disabled() -> None:
    ob = crypto_core.OrderBook(auto_reset_cvd=False)
    ob.apply_trade(100.0, 3.0, True, T0)
    ob.apply_trade(100.0, 1.0, True, T0 + 2 * DAY_MS)
    assert ob.cvd == pytest.approx(4.0)


def test_reset_cvd_and_full_reset(book: crypto_core.OrderBook) -> None:
    book.apply_trade(100.0, 2.0, True, T0)
    book.reset_cvd(10.0)
    assert book.cvd == 10.0
    assert book.buy_volume == 0.0

    book.reset()
    assert book.cvd == 0.0
    assert book.bid_levels == 0
    assert book.mid_price == 0.0
    assert book.update_count == 0
    # After reset() the next snapshot is treated as the first one again.
    assert book.update_book(_arr(BIDS), _arr(ASKS), T0) == 0.0


# ------------------------------------------------------------------ input checks


@pytest.mark.parametrize("depth", [0, crypto_core.MAX_DEPTH + 1])
def test_invalid_depth_raises(depth: int) -> None:
    with pytest.raises(ValueError, match="depth must be in"):
        crypto_core.OrderBook(depth=depth)


def test_wrong_array_shape_raises() -> None:
    ob = crypto_core.OrderBook()
    with pytest.raises(ValueError, match=r"shape \(n, 2\)"):
        ob.update_book(np.array([100.0, 1.0, 99.0]), _arr(ASKS), T0)


def test_hyperliquid_level_missing_key_raises() -> None:
    ob = crypto_core.OrderBook()
    with pytest.raises(KeyError):
        ob.update_hyperliquid([[{"px": "100.0"}], []], T0)
