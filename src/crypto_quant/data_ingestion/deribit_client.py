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
import pathlib
import time
from datetime import UTC, datetime
from typing import Final

import requests

from crypto_quant.config.settings import SETTINGS
from crypto_quant.quant_models.gex_calculator import (
    bs_gamma as _bs_gamma,
)
from crypto_quant.quant_models.gex_calculator import (
    compute_gex,
)
from crypto_quant.quant_models.gex_calculator import (
    norm_pdf as _norm_pdf,
)
from crypto_quant.quant_models.gex_calculator import (
    parse_deribit_instrument as _parse_instrument,
)

__all__ = [
    "DATA_DIR",
    "HISTORY_DIR",
    "HISTORY_INTERVAL_S",
    "OUTPUT_FILE",
    "POLL_INTERVAL_S",
    "_bs_gamma",
    "_norm_pdf",
    "_parse_instrument",
    "append_history",
    "compute_gex",
    "fetch_book_summary",
    "main",
    "should_append_history",
    "write_atomic",
]

DATA_DIR: Final[pathlib.Path] = pathlib.Path("data")
OUTPUT_FILE: Final[pathlib.Path] = DATA_DIR / "live_gex.json"
HISTORY_DIR: Final[pathlib.Path] = DATA_DIR / "gex_history"
HISTORY_INTERVAL_S: Final[float] = 60.0
POLL_INTERVAL_S: Final[int] = 10
RISK_FREE_RATE: Final[float] = 0.05
CONTRACT_SIZE_BTC: Final[float] = 1.0
REQUEST_TIMEOUT_S: Final[int] = 15
RATE_LIMIT_BACKOFF_S: Final[int] = 30
NETWORK_BACKOFF_S: Final[int] = 10


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


def write_atomic(data: dict, output_path: pathlib.Path) -> None:
    """Serialise data to JSON and replace output_path atomically via rename.

    Writing to a temporary sibling file before renaming ensures the reader
    never observes a partially written file. Includes Windows retry for file lock contention.
    """
    tmp_path = output_path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    for _ in range(5):
        try:
            tmp_path.replace(output_path)
            return
        except PermissionError:
            time.sleep(0.05)
    tmp_path.replace(output_path)


def append_history(payload: dict, history_dir: pathlib.Path) -> pathlib.Path:
    """Append one compact JSON line to the daily ``gex_YYYYMMDD.jsonl`` file.

    Unlike ``live_gex.json`` this is never overwritten, so a backtest can look up the
    snapshot that was valid at any past signal time.
    """
    history_dir.mkdir(parents=True, exist_ok=True)
    day: str = str(payload["timestamp"])[:10].replace("-", "")
    path: pathlib.Path = history_dir / f"gex_{day}.jsonl"
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
    return path


def should_append_history(last_written_s: float | None, now_s: float, interval_s: float = HISTORY_INTERVAL_S) -> bool:
    """Return True when no snapshot was stored yet or ``interval_s`` has elapsed."""
    return last_written_s is None or now_s - last_written_s >= interval_s


def main() -> None:
    """Entry point: poll Deribit in a loop and persist GEX snapshots."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Deribit GEX client started - writing to {OUTPUT_FILE}")

    last_history_s: float | None = None

    while True:
        try:
            summaries = fetch_book_summary()
            gex, spot = compute_gex(summaries)
            payload: dict = {
                "timestamp": datetime.now(tz=UTC).isoformat(),
                "spot": spot,
                "gex": gex,
            }
            write_atomic(payload, OUTPUT_FILE)
            now_s: float = time.monotonic()
            if should_append_history(last_history_s, now_s):
                append_history(payload, HISTORY_DIR)
                last_history_s = now_s
            ts = datetime.now(UTC).strftime("%H:%M:%S")
            print(f"[{ts}] GEX snapshot written - {len(gex)} strikes, spot ~ {spot:,.0f}")

        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            print(f"HTTP {status} error: {exc} - backing off {RATE_LIMIT_BACKOFF_S}s")
            time.sleep(RATE_LIMIT_BACKOFF_S)
            continue

        except requests.exceptions.RequestException as exc:
            print(f"Network error: {exc} - backing off {NETWORK_BACKOFF_S}s")
            time.sleep(NETWORK_BACKOFF_S)
            continue

        except (KeyError, ValueError) as exc:
            print(f"Data parsing error: {exc}")

        time.sleep(POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
