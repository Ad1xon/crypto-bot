"""Options pricing mathematics and Gamma Exposure (GEX) aggregation.

Provides pure mathematical functions for European option Greeks (Black-Scholes Gamma),
Deribit option ticker decoding, and per-strike GEX aggregation. Completely decoupled
from network I/O and filesystem writes.

GEX Convention:
--------------
Net GEX at a strike = (Call_OI x Call_Γ) - (Put_OI x Put_Γ)

- Positive net GEX: Dealers are net long gamma -> acts as price stabilizer / magnet.
- Negative net GEX: Dealers are net short gamma -> acts as price repellant / volatility amplifier.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Final

DEFAULT_RISK_FREE_RATE: Final[float] = 0.05
DEFAULT_CONTRACT_SIZE_BTC: Final[float] = 1.0


def norm_pdf(x: float) -> float:
    """Standard normal probability density function evaluated at x."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs_gamma(spot: float, strike: float, tau: float, sigma: float, r: float = DEFAULT_RISK_FREE_RATE) -> float:
    """Black-Scholes gamma for a European option.

    Parameters
    ----------
    spot:
        Current underlying asset price.
    strike:
        Option strike price.
    tau:
        Time to expiry in years (must be > 0).
    sigma:
        Annualised implied volatility as a decimal (e.g. 0.60 for 60% IV, must be > 0).
    r:
        Continuously compounded risk-free interest rate (default: 0.05).

    Returns
    -------
    float:
        Option gamma (d²V/dS²). Returns 0.0 whenever any input is non-positive,
        undefined, or arithmetic overflows.
    """
    if tau <= 0.0 or sigma <= 0.0 or spot <= 0.0 or strike <= 0.0:
        return 0.0
    try:
        sqrt_tau = math.sqrt(tau)
        d1 = (math.log(spot / strike) + (r + 0.5 * sigma**2) * tau) / (sigma * sqrt_tau)
        return norm_pdf(d1) / (spot * sigma * sqrt_tau)
    except (ValueError, ZeroDivisionError, OverflowError):
        return 0.0


def parse_deribit_instrument(name: str) -> tuple[float, str, datetime] | None:
    """Decode a Deribit option instrument name into (strike, option_type, expiry_utc).

    Deribit naming convention: BTC-DDMMMYY-STRIKE-TYPE
    Example: BTC-27DEC24-100000-C

    Parameters
    ----------
    name:
        Instrument name string.

    Returns
    -------
    tuple[float, str, datetime] | None:
        (strike, option_type, expiry_utc) or None if the format does not match.
    """
    parts = name.split("-")
    if len(parts) != 4:
        return None
    try:
        strike = float(parts[2])
        option_type = parts[3].upper()
        if option_type not in {"C", "P"}:
            return None
        # Deribit standard expiry time is 08:00:00 UTC on expiry date
        expiry_dt = datetime.strptime(parts[1].upper(), "%d%b%y").replace(
            hour=8, minute=0, second=0, microsecond=0, tzinfo=UTC
        )
        return strike, option_type, expiry_dt
    except (ValueError, IndexError):
        return None


def compute_gex(
    summaries: list[dict],
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    contract_size_btc: float = DEFAULT_CONTRACT_SIZE_BTC,
    now_utc: datetime | None = None,
) -> tuple[dict[str, float], float]:
    """Compute net GEX per strike from raw Deribit book summary records.

    For each record the Black-Scholes gamma is calculated individually so that
    options at the same strike but different expiries each contribute their correct
    gamma weighted by their own open interest and time-to-expiry.

    Parameters
    ----------
    summaries:
        List of book summary dictionaries (from Deribit get_book_summary_by_currency).
    risk_free_rate:
        Risk-free rate to use in Black-Scholes (default: 0.05).
    contract_size_btc:
        Size of 1 contract in BTC (default: 1.0).
    now_utc:
        Reference datetime in UTC. If None, uses datetime.now(tz=UTC).
        Passing a fixed timestamp enables deterministic unit testing and backtesting.

    Returns
    -------
    gex_by_strike:
        Mapping of strike (as string integer key) to net GEX value.
    spot:
        BTC spot price extracted from the first valid underlying_price field.
        Returns 0.0 if no valid instrument was found.
    """
    call_gex: dict[float, float] = {}
    put_gex: dict[float, float] = {}
    spot: float = 0.0
    ref_time = now_utc if now_utc is not None else datetime.now(tz=UTC)

    for item in summaries:
        instrument_name: str = item.get("instrument_name", "")
        parsed = parse_deribit_instrument(instrument_name)
        if parsed is None:
            continue

        strike, option_type, expiry_dt = parsed

        open_interest = float(item.get("open_interest") or 0.0)
        if open_interest <= 0.0:
            continue

        iv_raw = item.get("mark_iv")
        if iv_raw is None:
            continue
        sigma = float(iv_raw) / 100.0
        if sigma <= 0.0:
            continue

        raw_spot = item.get("underlying_price") or item.get("estimated_delivery_price")
        if raw_spot is None:
            continue
        underlying = float(raw_spot)
        if underlying <= 0.0:
            continue

        if spot == 0.0:
            spot = underlying

        tau = (expiry_dt - ref_time).total_seconds() / (365.25 * 24.0 * 3600.0)
        if tau <= 0.0:
            continue

        gamma = bs_gamma(underlying, strike, tau, sigma, risk_free_rate)
        contribution = gamma * open_interest * contract_size_btc

        if option_type == "C":
            call_gex[strike] = call_gex.get(strike, 0.0) + contribution
        else:
            put_gex[strike] = put_gex.get(strike, 0.0) + contribution

    all_strikes = set(call_gex.keys()) | set(put_gex.keys())
    gex_by_strike: dict[str, float] = {}

    for s in sorted(all_strikes):
        net = call_gex.get(s, 0.0) - put_gex.get(s, 0.0)
        gex_by_strike[str(int(s))] = net

    return gex_by_strike, spot
