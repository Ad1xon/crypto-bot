import pathlib

import pandas as pd
import pytest

from research.data_loader import load_merged_frame, load_parquet_glob


def test_load_parquet_glob_not_found(tmp_path: pathlib.Path):
    with pytest.raises(FileNotFoundError):
        load_parquet_glob(tmp_path, "nonexistent")


def test_load_merged_frame(tmp_path: pathlib.Path):
    # Create sample trades parquet
    trades_df = pd.DataFrame(
        {
            "timestamp_ms": [1000, 2000, 3000],
            "price": [50000.0, 50100.0, 50200.0],
            "qty": [0.1, 0.2, 0.3],
            "is_buyer_maker": [False, True, False],
        }
    )
    trades_file = tmp_path / "trades_sample_20260101T000000000.parquet"
    trades_df.to_parquet(trades_file)

    # Create sample bbo parquet
    bbo_df = pd.DataFrame(
        {
            "timestamp_ms": [1050, 2050, 3050],
            "best_bid_price": [49990.0, 50090.0, 50190.0],
            "best_ask_price": [50010.0, 50110.0, 50210.0],
            "best_bid_qty": [1.0, 1.5, 2.0],
            "best_ask_qty": [1.2, 1.1, 1.8],
            "spread": [20.0, 20.0, 20.0],
            "ofi": [-0.2, 0.4, 0.2],
        }
    )
    bbo_file = tmp_path / "bbo_sample_20260101T000000000.parquet"
    bbo_df.to_parquet(bbo_file)

    merged = load_merged_frame(tmp_path, include_vwap=True)

    assert len(merged) == 3
    assert "cvd" in merged.columns
    assert "vwap" in merged.columns
    assert "price" in merged.columns
    assert merged.iloc[0]["price"] == 50000.0
    assert merged.iloc[1]["price"] == 50100.0
