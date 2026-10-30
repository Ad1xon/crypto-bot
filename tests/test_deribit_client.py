import json
import pathlib

from crypto_quant.data_ingestion.deribit_client import (
    _bs_gamma,
    append_history,
    should_append_history,
)


def test_should_append_history():
    assert should_append_history(None, 100.0, 60.0) is True
    assert should_append_history(50.0, 100.0, 60.0) is False
    assert should_append_history(40.0, 100.0, 60.0) is True


def test_append_history(tmp_path: pathlib.Path):
    payload = {
        "timestamp": "2026-10-24T03:00:00+00:00",
        "spot": 85000.0,
        "gex": {"85000": 0.05, "86000": -0.02},
    }
    history_dir = tmp_path / "gex_history"
    out_file = append_history(payload, history_dir)

    assert out_file == history_dir / "gex_20261024.jsonl"
    assert out_file.exists()

    with open(out_file, "r", encoding="utf-8") as fh:
        lines = fh.readlines()
    assert len(lines) == 1
    data = json.loads(lines[0])
    assert data["spot"] == 85000.0
    assert data["gex"]["85000"] == 0.05


def test_bs_gamma_edge_cases():
    assert _bs_gamma(spot=0.0, strike=100.0, tau=0.1, sigma=0.5, r=0.05) == 0.0
    assert _bs_gamma(spot=100.0, strike=0.0, tau=0.1, sigma=0.5, r=0.05) == 0.0
    assert _bs_gamma(spot=100.0, strike=100.0, tau=0.0, sigma=0.5, r=0.05) == 0.0
    assert _bs_gamma(spot=100.0, strike=100.0, tau=0.1, sigma=0.0, r=0.05) == 0.0
    gamma = _bs_gamma(spot=50000.0, strike=50000.0, tau=0.1, sigma=0.6, r=0.05)
    assert gamma > 0.0
