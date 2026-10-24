import json
import pathlib

from quant_models.gex_risk_manager import (
    GexHistory,
    GexRiskManager,
    load_gex_map_from_file,
)


def test_gex_risk_manager_static_map():
    mgr = GexRiskManager(gex_map={50000.0: 1.5, 51000.0: -0.5})
    assert mgr.get_gex_map() == {50000.0: 1.5, 51000.0: -0.5}
    assert mgr.evaluate_signal(50020.0, "BULLISH_ABSORPTION") is True
    assert mgr.evaluate_signal(50980.0, "BULLISH_ABSORPTION") is False


def test_gex_risk_manager_from_file(tmp_path: pathlib.Path):
    file_path = tmp_path / "live_gex.json"
    payload = {
        "timestamp": "2026-10-24T03:00:00+00:00",
        "spot": 50000.0,
        "gex": {"50000": 1.2, "51000": -0.8},
    }
    with open(file_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)

    # Fresh file
    mgr = GexRiskManager(data_file=file_path)
    loaded = mgr.get_gex_map()
    assert loaded == {50000.0: 1.2, 51000.0: -0.8}

    # Helper function load_gex_map_from_file
    assert load_gex_map_from_file(file_path) == {50000.0: 1.2, 51000.0: -0.8}


def test_gex_history_lookup():
    snapshots = [
        (10_000, {50000.0: 1.0}),
        (20_000, {50000.0: 2.0}),
        (30_000, {50000.0: 3.0}),
    ]
    history = GexHistory(snapshots, max_age_s=15.0)  # 15s max age = 15,000ms

    # Query before first snapshot -> empty
    assert history.map_at(5_000) == {}

    # Query exactly at snapshot 1
    assert history.map_at(10_000) == {50000.0: 1.0}

    # Query between snapshot 1 and 2 (e.g. at 15,000) -> returns snapshot 1
    assert history.map_at(15_000) == {50000.0: 1.0}

    # Query between snapshot 2 and 3 -> returns snapshot 2
    assert history.map_at(25_000) == {50000.0: 2.0}

    # Query past snapshot 3 within max_age (35_000 -> age 5,000 <= 15,000)
    assert history.map_at(35_000) == {50000.0: 3.0}

    # Query past snapshot 3 exceeding max_age (50_000 -> age 20,000 > 15,000) -> empty
    assert history.map_at(50_000) == {}


def test_gex_history_from_dir(tmp_path: pathlib.Path):
    history_dir = tmp_path / "gex_history"
    history_dir.mkdir()

    file1 = history_dir / "gex_20261024.jsonl"
    with open(file1, "w", encoding="utf-8") as fh:
        fh.write(
            json.dumps(
                {
                    "timestamp": "2026-10-24T03:00:00+00:00",
                    "spot": 50000.0,
                    "gex": {"50000": 1.0},
                }
            )
            + "\n"
        )
        fh.write("corrupted json line\n")
        fh.write(
            json.dumps(
                {
                    "timestamp": "2026-10-24T03:01:00+00:00",
                    "spot": 50100.0,
                    "gex": {"50000": 1.5},
                }
            )
            + "\n"
        )

    history = GexHistory.from_dir(history_dir)
    assert len(history) == 2
