"""Super-confluence entry gate: VWAP side-alignment AND nearest-strike GEX.

Shared by the live execution engine and the event backtester so both
environments apply identical entry predicates (DRY).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

_BULLISH: Final[str] = "BULLISH_ABSORPTION"
_BEARISH: Final[str] = "BEARISH_ABSORPTION"


@dataclass(frozen=True)
class ConfluenceVerdict:
    """Result of a single VWAP + GEX confluence evaluation."""

    allowed: bool
    vwap_ok: bool
    gex_ok: bool
    nearest_strike: float | None
    gex_value: float


class SessionVwap:
    """Incremental session VWAP accumulator shared by live trade handlers."""

    def __init__(self) -> None:
        self._cum_pv: float = 0.0
        self._cum_v: float = 0.0

    def update(self, price: float, qty: float) -> None:
        """Fold one trade print into the running volume-weighted average."""
        if price <= 0.0 or qty <= 0.0:
            return
        self._cum_pv += price * qty
        self._cum_v += qty

    def reset(self) -> None:
        """Clear cumulative price-volume state."""
        self._cum_pv = 0.0
        self._cum_v = 0.0

    @property
    def value(self) -> float | None:
        """Return current VWAP, or ``None`` before any qualifying volume."""
        if self._cum_v <= 0.0:
            return None
        return self._cum_pv / self._cum_v


class SuperConfluenceFilter:
    """Require absorption signals to clear both VWAP and GEX confluence.

    Bullish: ``price >= vwap`` and nearest-strike net GEX ``> 0``.
    Bearish: ``price <= vwap`` and nearest-strike net GEX ``< 0``.
    Missing VWAP or an empty GEX map always rejects the entry.
    """

    @staticmethod
    def vwap_aligned(signal_type: str, price: float, vwap: float) -> bool:
        """Return whether *price* is on the correct side of *vwap* for *signal_type*."""
        if not math.isfinite(vwap):
            return False
        if signal_type == _BULLISH:
            return price >= vwap
        if signal_type == _BEARISH:
            return price <= vwap
        return False

    @staticmethod
    def gex_aligned(signal_type: str, gex_value: float) -> bool:
        """Return whether nearest-strike *gex_value* supports *signal_type*."""
        if signal_type == _BULLISH:
            return gex_value > 0.0
        if signal_type == _BEARISH:
            return gex_value < 0.0
        return False

    @staticmethod
    def nearest_strike_gex(
        gex_map: dict[float, float],
        price: float,
    ) -> tuple[float, float] | None:
        """Return ``(nearest_strike, gex)`` for *price*, or ``None`` if *gex_map* is empty."""
        if not gex_map:
            return None
        nearest: float = min(gex_map.keys(), key=lambda strike: abs(strike - price))
        return nearest, gex_map[nearest]

    def evaluate(
        self,
        signal_type: str,
        price: float,
        vwap: float | None,
        gex_map: dict[float, float],
    ) -> ConfluenceVerdict:
        """Gate an absorption signal on VWAP alignment and nearest-strike GEX sign."""
        vwap_ok: bool = vwap is not None and self.vwap_aligned(signal_type, price, vwap)
        ctx: tuple[float, float] | None = self.nearest_strike_gex(gex_map, price)
        if ctx is None:
            return ConfluenceVerdict(
                allowed=False,
                vwap_ok=vwap_ok,
                gex_ok=False,
                nearest_strike=None,
                gex_value=0.0,
            )
        nearest_strike, gex_value = ctx
        gex_ok: bool = self.gex_aligned(signal_type, gex_value)
        return ConfluenceVerdict(
            allowed=vwap_ok and gex_ok,
            vwap_ok=vwap_ok,
            gex_ok=gex_ok,
            nearest_strike=nearest_strike,
            gex_value=gex_value,
        )
