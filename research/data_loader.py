"""Shared tick data loader and preprocessor for research and backtesting.

Provides reusable utilities to ingest and align parquet tick data
(BBO snapshots + trade ticks) into merged, continuous time-series frames.
"""

from __future__ import annotations

import pathlib
from typing import Final

import pandas as pd

_DEFAULT_DATA_DIR: Final[pathlib.Path] = pathlib.Path("data")


def load_parquet_glob(data_dir: pathlib.Path, prefix: str) -> pd.DataFrame:
    """Concatenate every Parquet file whose name starts with *prefix*.

    Files are sorted chronologically by filename. The concatenated result is
    sorted by ``timestamp_ms`` and returned with a clean integer index.

    Raises:
        FileNotFoundError: When no matching files exist in *data_dir*.
    """
    files: list[pathlib.Path] = sorted(data_dir.glob(f"{prefix}_*.parquet"))
    if not files:
        raise FileNotFoundError(f"No Parquet files with prefix '{prefix}' found in '{data_dir}'")
    return (
        pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        .sort_values("timestamp_ms", kind="stable")
        .reset_index(drop=True)
    )


def load_merged_frame(data_dir: pathlib.Path = _DEFAULT_DATA_DIR, include_vwap: bool = True) -> pd.DataFrame:
    """Build a merged BBO + Trade DataFrame aligned by timestamp at 100 ms cadence.

    Pipeline:
    1. Load all trade Parquet files; compute running signed CVD as
       ``cumsum`` of ``+qty`` (taker buy) / ``-qty`` (taker sell).
    2. Optionally calculate running cumulative VWAP.
    3. Load all BBO Parquet files; filter warmup rows where ``best_ask_price <= 0``.
    4. Align via ``pd.merge_asof`` (BBO left, trades right, backward fill)
       so every BBO tick is enriched with the most recent trade price, CVD, and VWAP.

    Returns:
        DataFrame with columns: ``timestamp_ms``, ``dt``, ``best_bid_price``,
        ``best_ask_price``, ``best_bid_qty``, ``best_ask_qty``, ``spread``,
        ``ofi``, ``price``, ``cvd`` (and ``vwap`` if *include_vwap* is True).
    """
    trades: pd.DataFrame = load_parquet_glob(data_dir, "trades")
    trades["signed_qty"] = trades["qty"].where(~trades["is_buyer_maker"], -trades["qty"])
    trades["cvd"] = trades["signed_qty"].cumsum()
    trades["dt"] = pd.to_datetime(trades["timestamp_ms"], unit="ms", utc=True)

    merge_cols: list[str] = ["timestamp_ms", "price", "cvd"]

    if include_vwap:
        vol_col: str | None = next(
            (c for c in ("qty", "q", "volume") if c in trades.columns),
            None,
        )
        if vol_col is None and "price" in trades.columns and "quote_qty" in trades.columns:
            trades["_vol"] = trades["quote_qty"].abs() / trades["price"]
            vol_col = "_vol"
        elif vol_col is None:
            trades["_vol"] = trades["price"].abs() * 0 + 1
            vol_col = "_vol"

        cum_pv: pd.Series = (trades["price"] * trades[vol_col]).cumsum()
        cum_v: pd.Series = trades[vol_col].cumsum()
        trades["vwap"] = cum_pv / cum_v
        merge_cols.append("vwap")

    bbo: pd.DataFrame = load_parquet_glob(data_dir, "bbo")
    bbo = bbo[bbo["best_ask_price"] > 0].reset_index(drop=True)
    bbo["dt"] = pd.to_datetime(bbo["timestamp_ms"], unit="ms", utc=True)

    return pd.merge_asof(
        bbo,
        trades[merge_cols],
        on="timestamp_ms",
        direction="backward",
    )


def calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range (ATR) from OHLC or price time-series.

    If standard 'high', 'low', 'close' columns are present, uses exact True Range.
    Otherwise derives True Range from sequential price differences.
    """
    high = df["high"] if "high" in df.columns else df["price"]
    low = df["low"] if "low" in df.columns else df["price"]
    close = df["close"] if "close" in df.columns else df["price"]

    prev_close = close.shift(1).fillna(close)
    tr1 = (high - low).abs()
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(window=period, min_periods=1).mean()
