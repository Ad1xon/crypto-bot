"""Deribit public options data client for real-time BTC Gamma Exposure profiling.

Polls Deribit's get_book_summary_by_currency endpoint every POLL_INTERVAL_S
seconds, derives per-strike GEX from Black-Scholes gamma and open interest,
then writes the result atomically to data/live_gex.json so a separate
visualizer process can read it without encountering partial writes.

GEX convention
--------------
Net GEX at a strike = (Call_OI x Call_Γ) - (Put_OI x Put_Γ)

Positive net GEX → dealers are net long gamma → acts as a price magnet.
Negative net GEX → dealers are net short gamma → acts as a price repellant.

Black-Scholes gamma is computed locally from mark_iv (annualised implied
volatility as a percentage), so no per-instrument ticker call is needed.
"""

from __future__ import annotations

import json
import math
import pathlib
import time
from datetime import datetime, timezone
from typing import Final

import requests

from config.settings import SETTINGS

DATA_DIR: Final[pathlib.Path] = pathlib.Path("data")
OUTPUT_FILE: Final[pathlib.Path] = DATA_DIR / "live_gex.json"
POLL_INTERVAL_S: Final[int] = 10
RISK_FREE_RATE: Final[float] = 0.05
CONTRACT_SIZE_BTC: Final[float] = 1.0
REQUEST_TIMEOUT_S: Final[int] = 15
RATE_LIMIT_BACKOFF_S: Final[int] = 30
NETWORK_BACKOFF_S: Final[int] = 10


def _norm_pdf(x: float) -> float:
    """Standard normal probability density function evaluated at x."""
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_gamma(spot: float, strike: float, tau: float, sigma: float, r: float) -> float:
    """Black-Scholes gamma for a European option.

    Parameters
    ----------
    spot:   Current underlying price.
    strike: Option strike price.
    tau:    Time to expiry in years (must be > 0).
    sigma:  Annualised implied volatility as a decimal (must be > 0).
    r:      Continuously compounded risk-free rate.

    Returns 0.0 whenever any input is non-positive or arithmetic would fail.
    """
    if tau <= 0.0 or sigma <= 0.0 or spot <= 0.0 or strike <= 0.0:
        return 0.0
    try:
        d1 = (
            math.log(spot / strike) + (r + 0.5 * sigma ** 2) * tau
        ) / (sigma * math.sqrt(tau))
        return _norm_pdf(d1) / (spot * sigma * math.sqrt(tau))
    except (ValueError, ZeroDivisionError):
        return 0.0


def _parse_instrument(
    name: str,
) -> tuple[float, str, datetime] | None:
    """Decode a Deribit instrument name into (strike, option_type, expiry_utc).

    Deribit naming convention: BTC-DDMMMYY-STRIKE-TYPE
    Example: BTC-27DEC24-100000-C

    Returns None if the name does not match the expected pattern.
    """
    parts = name.split("-")
    if len(parts) != 4:
        return None
    try:
        strike = float(parts[2])
        option_type = parts[3].upper()
        if option_type not in {"C", "P"}:
            return None
        expiry_dt = datetime.strptime(parts[1].upper(), "%d%b%y").replace(
            hour=8, minute=0, second=0, tzinfo=timezone.utc
        )
        return strike, option_type, expiry_dt
    except (ValueError, IndexError):
        return None


def fetch_book_summary() -> list[dict]:
    """Request all BTC option book summaries from the Deribit public REST API.

    Raises
    ------
    requests.exceptions.HTTPError
        Propagated on non-2xx responses so the caller can apply back-off.
    requests.exceptions.RequestException
        Propagated on network-level failures.
    """
    url = f"{SETTINGS.deribit.base_url}/public/get_book_summary_by_currency"
    params: dict[str, str] = {"currency": "BTC", "kind": "option"}
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_S)
    response.raise_for_status()
    payload: dict = response.json()
    return payload.get("result", [])


def compute_gex(
    summaries: list[dict],
) -> tuple[dict[str, float], float]:
    """Compute net GEX per strike from raw Deribit book summary records.

    For each record the BS gamma is calculated individually so that options
    at the same strike but different expiries each contribute their correct
    gamma weighted by their own open interest and time-to-expiry.

    Parameters
    ----------
    summaries:
        Raw list returned by :func:`fetch_book_summary`.

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
    now_utc = datetime.now(tz=timezone.utc)

    for item in summaries:
        instrument_name: str = item.get("instrument_name", "")
        parsed = _parse_instrument(instrument_name)
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

        tau = (expiry_dt - now_utc).total_seconds() / (365.25 * 24.0 * 3600.0)
        if tau <= 0.0:
            continue

        gamma = _bs_gamma(underlying, strike, tau, sigma, RISK_FREE_RATE)
        contribution = gamma * open_interest * CONTRACT_SIZE_BTC

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


def write_atomic(data: dict, output_path: pathlib.Path) -> None:
    """Serialise data to JSON and replace output_path atomically via rename.

    Writing to a temporary sibling file before renaming ensures the reader
    never observes a partially written file.
    """
    tmp_path = output_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    tmp_path.replace(output_path)


def main() -> None:
    """Entry point: poll Deribit in a loop and persist GEX snapshots."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Deribit GEX client started — writing to {OUTPUT_FILE}")

    while True:
        try:
            summaries = fetch_book_summary()
            gex, spot = compute_gex(summaries)
            payload: dict = {
                "timestamp": datetime.now(tz=timezone.utc).isoformat(),
                "spot": spot,
                "gex": gex,
            }
            write_atomic(payload, OUTPUT_FILE)
            ts = datetime.now().strftime("%H:%M:%S")
            print(f"[{ts}] GEX snapshot written — {len(gex)} strikes, spot ≈ {spot:,.0f}")

        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            print(f"HTTP {status} error: {exc} — backing off {RATE_LIMIT_BACKOFF_S}s")
            time.sleep(RATE_LIMIT_BACKOFF_S)
            continue

        except requests.exceptions.RequestException as exc:
            print(f"Network error: {exc} — backing off {NETWORK_BACKOFF_S}s")
            time.sleep(NETWORK_BACKOFF_S)
            continue

        except (KeyError, ValueError, json.JSONDecodeError) as exc:
            print(f"Data parsing error: {exc}")

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
