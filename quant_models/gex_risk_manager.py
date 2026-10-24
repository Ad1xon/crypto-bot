"""GEX snapshot loader and nearest-strike lookup for SMC entry confluence.

Reads the live Gamma Exposure snapshot written by
``data_ingestion/deribit_client.py``. Sign-alignment rules live in
:class:`~quant_models.super_confluence.SuperConfluenceFilter` so live and
backtest share one predicate.
"""

from __future__ import annotations

import bisect
import json
import logging
import os
import pathlib
import time
from datetime import datetime
from typing import Final

from quant_models.super_confluence import SuperConfluenceFilter

_LOG: Final[logging.Logger] = logging.getLogger(__name__)
_GEX_MAX_AGE_S: Final[int] = 30
_HISTORY_MAX_AGE_S: Final[float] = 300.0


class GexHistory:
    """Point-in-time GEX lookup over the snapshots stored by ``deribit_client``.

    ``map_at`` returns the latest snapshot at or before the requested timestamp, so a
    backtest never sees GEX data from the future. Gaps longer than ``max_age_s`` yield an
    empty map, mirroring the live staleness guard.
    """

    def __init__(
        self,
        snapshots: list[tuple[int, dict[float, float]]],
        max_age_s: float = _HISTORY_MAX_AGE_S,
    ) -> None:
        ordered = sorted(snapshots, key=lambda item: item[0])
        self._timestamps_ms: list[int] = [ts for ts, _ in ordered]
        self._maps: list[dict[float, float]] = [gex for _, gex in ordered]
        self._max_age_ms: float = max_age_s * 1000.0

    def __len__(self) -> int:
        return len(self._timestamps_ms)

    @classmethod
    def from_dir(cls, history_dir: pathlib.Path, max_age_s: float = _HISTORY_MAX_AGE_S) -> GexHistory:
        """Load every ``*.jsonl`` file in *history_dir*; malformed lines are skipped."""
        snapshots: list[tuple[int, dict[float, float]]] = []
        for path in sorted(history_dir.glob("*.jsonl")):
            with open(path, "r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        payload: dict = json.loads(line)
                        ts_ms: int = int(datetime.fromisoformat(payload["timestamp"]).timestamp() * 1000)
                        gex: dict[float, float] = {float(k): float(v) for k, v in payload["gex"].items()}
                    except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError):
                        continue
                    snapshots.append((ts_ms, gex))
        return cls(snapshots, max_age_s)

    def map_at(self, timestamp_ms: float) -> dict[float, float]:
        """Return the GEX map valid at *timestamp_ms*, or ``{}`` if none or too old."""
        idx: int = bisect.bisect_right(self._timestamps_ms, timestamp_ms) - 1
        if idx < 0:
            return {}
        if timestamp_ms - self._timestamps_ms[idx] > self._max_age_ms:
            return {}
        return self._maps[idx]


class GexRiskManager:
    """Load dealer GEX maps for :class:`SuperConfluenceFilter` evaluation.

    Parameters
    ----------
    data_file:
        Path to the JSON snapshot produced by
        ``data_ingestion/deribit_client.py``.
    gex_map:
        Optional in-memory strike → net GEX map. When provided, disk I/O and
        staleness checks are skipped (used by the event backtester).
    """

    def __init__(
        self,
        data_file: pathlib.Path = pathlib.Path("data/live_gex.json"),
        gex_map: dict[float, float] | None = None,
    ) -> None:
        self._data_file: pathlib.Path = data_file
        self._static_map: dict[float, float] | None = gex_map
        self._confluence: SuperConfluenceFilter = SuperConfluenceFilter()

    def get_gex_map(self) -> dict[float, float]:
        """Return the active GEX map (static override or latest disk snapshot)."""
        if self._static_map is not None:
            return self._static_map
        return self._load_latest_gex()

    def _load_latest_gex(self) -> dict[float, float]:
        """Read the latest GEX snapshot from disk.

        Returns a mapping of ``strike (float) → net_gex (float)``.
        Returns an empty dict whenever the file is absent, stale, corrupt,
        or has an unexpected schema.
        """
        try:
            file_age: float = time.time() - os.path.getmtime(self._data_file)
            if file_age > _GEX_MAX_AGE_S:
                _LOG.critical(
                    "GEX data is stale (%.0fs old). Deribit client might be offline.",
                    file_age,
                )
                return {}
            with open(self._data_file, "r", encoding="utf-8") as fh:
                payload: dict = json.load(fh)
            raw_gex: dict = payload.get("gex", {})
            return {float(k): float(v) for k, v in raw_gex.items()}
        except FileNotFoundError:
            _LOG.debug(
                "GEX file not found at %s — returning empty snapshot.",
                self._data_file,
            )
            return {}
        except json.JSONDecodeError as exc:
            _LOG.warning(
                "GEX file corrupt or partially written (%s) — skipping filter.",
                exc,
            )
            return {}
        except (KeyError, ValueError) as exc:
            _LOG.warning("Unexpected GEX schema (%s) — skipping filter.", exc)
            return {}

    def nearest_strike_info(self, price: float) -> tuple[float, float] | None:
        """Return ``(nearest_strike, gex_value)`` for *price*, or ``None`` if unavailable."""
        return self._confluence.nearest_strike_gex(self.get_gex_map(), price)

    def evaluate_signal(self, current_price: float, signal_type: str) -> bool:
        """Return whether nearest-strike GEX alone supports *signal_type*.

        Prefer :meth:`SuperConfluenceFilter.evaluate` for live/backtest entries;
        this method remains for sizing/diagnostics that only need the GEX leg.
        """
        gex_map: dict[float, float] = self.get_gex_map()
        if not gex_map:
            _LOG.warning(
                "GEX Filter: signal %s at %.2f BLOCKED (No valid GEX data).",
                signal_type,
                current_price,
            )
            return False

        ctx: tuple[float, float] | None = self._confluence.nearest_strike_gex(gex_map, current_price)
        if ctx is None:
            return False

        nearest, gex_value = ctx
        allowed: bool = self._confluence.gex_aligned(signal_type, gex_value)
        verdict: str = "ALLOWED" if allowed else "BLOCKED"
        _LOG.debug(
            "GEX Filter: Price %.2f -> Nearest Strike %.0f (GEX: %.6f) -> %s signal %s",
            current_price,
            nearest,
            gex_value,
            signal_type,
            verdict,
        )
        return allowed


def load_gex_map_from_file(path: pathlib.Path) -> dict[float, float]:
    """Load a strike → net GEX map from a Deribit-style JSON snapshot (no age check)."""
    with open(path, "r", encoding="utf-8") as fh:
        payload: dict = json.load(fh)
    raw_gex: dict = payload.get("gex", {})
    return {float(k): float(v) for k, v in raw_gex.items()}
