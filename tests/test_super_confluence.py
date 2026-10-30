"""Unit tests for SuperConfluenceFilter and SessionVwap."""

from __future__ import annotations

import math

import pytest

from crypto_quant.quant_models.super_confluence import (
    SessionVwap,
    SuperConfluenceFilter,
)

_BULLISH: str = "BULLISH_ABSORPTION"
_BEARISH: str = "BEARISH_ABSORPTION"


@pytest.fixture
def confluence() -> SuperConfluenceFilter:
    return SuperConfluenceFilter()


@pytest.fixture
def gex_map() -> dict[float, float]:
    return {
        64_000.0: -0.5,
        65_000.0: 1.2,
        66_000.0: -0.3,
    }


class TestSessionVwap:
    def test_value_none_before_trades(self) -> None:
        assert SessionVwap().value is None

    def test_incremental_vwap(self) -> None:
        vwap = SessionVwap()
        vwap.update(100.0, 2.0)
        vwap.update(200.0, 2.0)
        assert vwap.value == pytest.approx(150.0)

    def test_ignores_non_positive_prints(self) -> None:
        vwap = SessionVwap()
        vwap.update(100.0, 0.0)
        vwap.update(-1.0, 1.0)
        assert vwap.value is None


class TestVwapAlignment:
    def test_bullish_requires_price_at_or_above_vwap(self, confluence: SuperConfluenceFilter) -> None:
        assert confluence.vwap_aligned(_BULLISH, 100.0, 100.0) is True
        assert confluence.vwap_aligned(_BULLISH, 101.0, 100.0) is True
        assert confluence.vwap_aligned(_BULLISH, 99.0, 100.0) is False

    def test_bearish_requires_price_at_or_below_vwap(self, confluence: SuperConfluenceFilter) -> None:
        assert confluence.vwap_aligned(_BEARISH, 100.0, 100.0) is True
        assert confluence.vwap_aligned(_BEARISH, 99.0, 100.0) is True
        assert confluence.vwap_aligned(_BEARISH, 101.0, 100.0) is False

    def test_nan_vwap_rejects(self, confluence: SuperConfluenceFilter) -> None:
        assert confluence.vwap_aligned(_BULLISH, 100.0, math.nan) is False


class TestGexAlignment:
    def test_bullish_needs_positive_gex(self, confluence: SuperConfluenceFilter) -> None:
        assert confluence.gex_aligned(_BULLISH, 0.1) is True
        assert confluence.gex_aligned(_BULLISH, -0.1) is False
        assert confluence.gex_aligned(_BULLISH, 0.0) is False

    def test_bearish_needs_negative_gex(self, confluence: SuperConfluenceFilter) -> None:
        assert confluence.gex_aligned(_BEARISH, -0.1) is True
        assert confluence.gex_aligned(_BEARISH, 0.1) is False
        assert confluence.gex_aligned(_BEARISH, 0.0) is False


class TestEvaluate:
    def test_bullish_requires_both_legs(
        self,
        confluence: SuperConfluenceFilter,
        gex_map: dict[float, float],
    ) -> None:
        allowed = confluence.evaluate(_BULLISH, 65_050.0, 65_000.0, gex_map)
        assert allowed.allowed is True
        assert allowed.vwap_ok is True
        assert allowed.gex_ok is True
        assert allowed.nearest_strike == 65_000.0

        below_vwap = confluence.evaluate(_BULLISH, 64_900.0, 65_000.0, gex_map)
        assert below_vwap.allowed is False
        assert below_vwap.vwap_ok is False

        neg_gex = confluence.evaluate(_BULLISH, 64_100.0, 64_000.0, gex_map)
        assert neg_gex.allowed is False
        assert neg_gex.gex_ok is False

    def test_bearish_requires_both_legs(
        self,
        confluence: SuperConfluenceFilter,
        gex_map: dict[float, float],
    ) -> None:
        allowed = confluence.evaluate(_BEARISH, 64_000.0, 64_100.0, gex_map)
        assert allowed.allowed is True
        assert allowed.vwap_ok is True
        assert allowed.gex_ok is True

        above_vwap = confluence.evaluate(_BEARISH, 64_200.0, 64_100.0, gex_map)
        assert above_vwap.allowed is False

    def test_empty_gex_map_blocks(self, confluence: SuperConfluenceFilter) -> None:
        verdict = confluence.evaluate(_BULLISH, 65_000.0, 64_000.0, {})
        assert verdict.allowed is False
        assert verdict.gex_ok is False
        assert verdict.nearest_strike is None

    def test_missing_vwap_blocks(
        self,
        confluence: SuperConfluenceFilter,
        gex_map: dict[float, float],
    ) -> None:
        verdict = confluence.evaluate(_BULLISH, 65_050.0, None, gex_map)
        assert verdict.allowed is False
        assert verdict.vwap_ok is False
