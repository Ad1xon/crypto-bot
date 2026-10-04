"""Smart Money Concept (SMC) microstructure signal detectors.

Operates on the merged BBO + Trade DataFrame produced by the research data
pipeline.  All computations use vectorised pandas operations — no Python-level
row iteration — to preserve performance at tick-level data volumes.

Absorption / Divergence logic
------------------------------
BULLISH_ABSORPTION
    CVD delta  < -z_threshold * cvd_std  (heavy aggressive sell flow)
    Price delta > -price_tolerance * price_std  (price holds / barely falls)
    ofi_smooth > 0.0                            (passive limit buying present)
    Interpretation: limit-order buyers absorb the sell flow; price is sticky
    at support while CVD dives and the smoothed OFI confirms passive bid-side
    support — a bullish leading indicator.

BEARISH_ABSORPTION
    CVD delta  > +z_threshold * cvd_std  (heavy aggressive buy flow)
    Price delta < +price_tolerance * price_std  (price holds / barely rises)
    ofi_smooth < 0.0                            (passive limit selling present)
    Interpretation: limit-order sellers absorb the buy flow; price fails to
    follow CVD while the smoothed OFI confirms passive ask-side resistance —
    a bearish leading indicator.
"""

from __future__ import annotations

import time
from typing import Final

import pandas as pd

_BULLISH_LABEL: Final[str] = "BULLISH_ABSORPTION"
_BEARISH_LABEL: Final[str] = "BEARISH_ABSORPTION"
_SIGNAL_COLUMNS: Final[list[str]] = [
    "timestamp_ms", "dt", "price", "cvd", "ofi_smooth",
    "price_delta", "cvd_delta", "signal", "signal_ts",
]


class MicrostructureDetector:
    """Detect Smart Money absorption patterns in merged tick DataFrames.

    Responsibilities:
    - Smooth noisy raw OFI via a configurable rolling mean window.
    - Detect CVD / Price divergences indicating institutional absorption.
    - Collapse consecutive same-type signal clusters to one peak event each.

    All public methods accept and return DataFrames.  The class itself is
    stateless beyond the configuration parameters set at construction time,
    making instances reusable across multiple DataFrames.
    """

    def __init__(
        self,
        ofi_smooth_window: int = 50,
        divergence_window: int = 100,
        cvd_z_threshold: float = 2.0,
        price_tolerance: float = 0.5,
    ) -> None:
        """Construct the detector with configurable window and threshold values.

        Parameters
        ----------
        ofi_smooth_window:
            Rolling mean window for OFI smoothing in ticks.
            At 100 ms cadence, 50 ticks = 5 seconds of averaging.
        divergence_window:
            Look-back window in ticks for computing CVD and price deltas.
            100 ticks = 10 seconds of context at 100 ms cadence.
        cvd_z_threshold:
            Minimum z-score magnitude that a CVD delta must exceed to qualify
            as a divergence signal.  2.0 catches the outer ~5 % of moves.
        price_tolerance:
            Maximum price delta z-score magnitude allowed for a signal to be
            counted as divergence.  0.5 means price must stay within half a
            standard deviation of its mean change.
        """
        self._ofi_smooth_window: int = ofi_smooth_window
        self._divergence_window: int = divergence_window
        self._cvd_z_threshold: float = cvd_z_threshold
        self._price_tolerance: float = price_tolerance

    def smooth_ofi(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return a copy of *df* with an ``ofi_smooth`` column appended.

        Applies a causal rolling mean (``min_periods=1``) to the raw ``ofi``
        column to suppress HFT noise without introducing future look-ahead
        bias.  The smoothed series reacts to structural shifts within
        ``ofi_smooth_window`` ticks while filtering tick-to-tick jitter.
        """
        result: pd.DataFrame = df.copy()
        result["ofi_smooth"] = (
            result["ofi"]
            .rolling(window=self._ofi_smooth_window, min_periods=1)
            .mean()
        )
        return result

    def detect_cvd_divergence(self, df: pd.DataFrame) -> pd.DataFrame:
        """Scan the merged tick frame for CVD / Price divergence anomalies.

        The full vectorised pipeline:

        1. Compute ``price_delta`` and ``cvd_delta`` as ``Series.diff(window)``
           — O(n) with no Python iteration.
        2. Z-score-normalise each delta series against its own distribution so
           thresholds are adaptive to the session's volatility regime.
        3. Apply boolean masks to identify bullish and bearish absorption rows.
        4. Tag each detected row with its signal label.
        5. Deduplicate consecutive same-type clusters (see
           :meth:`_deduplicate_signals`) so each absorption event appears once.

        If ``ofi_smooth`` is absent from *df*, :meth:`smooth_ofi` is called
        automatically before scanning.

        Returns a DataFrame containing only the flagged signal rows.  Columns:
        ``timestamp_ms``, ``dt``, ``price``, ``cvd``, ``ofi_smooth``,
        ``price_delta``, ``cvd_delta``, ``signal``, ``signal_ts``.  An empty
        DataFrame with the same schema is returned when no signals are found
        or when the delta distributions are degenerate (zero variance).

        ``signal_ts`` is a ``time.perf_counter()`` monotonic stamp taken at the
        moment absorption signals are materialised — the start of the live
        latency stack (signal → fill).
        """
        working: pd.DataFrame = (
            df if "ofi_smooth" in df.columns else self.smooth_ofi(df)
        )

        price_delta: pd.Series = working["price"].diff(self._divergence_window)
        cvd_delta: pd.Series = working["cvd"].diff(self._divergence_window)

        cvd_std: float = float(cvd_delta.std())
        price_std: float = float(price_delta.std())

        if cvd_std == 0.0 or price_std == 0.0:
            return pd.DataFrame(columns=_SIGNAL_COLUMNS)

        cvd_z: pd.Series = cvd_delta / cvd_std
        price_z: pd.Series = price_delta / price_std

        bullish_mask: pd.Series = (
            (cvd_z < -self._cvd_z_threshold)
            & (price_z > -self._price_tolerance)
            & (working["ofi_smooth"] > 0.0)
        )
        bearish_mask: pd.Series = (
            (cvd_z > self._cvd_z_threshold)
            & (price_z < self._price_tolerance)
            & (working["ofi_smooth"] < 0.0)
        )

        signal_col: pd.Series = pd.Series("", index=working.index, dtype=object)
        signal_col = signal_col.mask(bullish_mask, _BULLISH_LABEL)
        signal_col = signal_col.mask(bearish_mask, _BEARISH_LABEL)

        detected: pd.Series = bullish_mask | bearish_mask

        base_cols: list[str] = ["timestamp_ms", "dt", "price", "cvd", "ofi_smooth"]
        result: pd.DataFrame = working.loc[detected, base_cols].copy()
        result["price_delta"] = price_delta.loc[detected].to_numpy()
        result["cvd_delta"] = cvd_delta.loc[detected].to_numpy()
        result["signal"] = signal_col.loc[detected].to_numpy()

        deduped: pd.DataFrame = self._deduplicate_signals(result).reset_index(drop=True)
        if not deduped.empty:
            deduped["signal_ts"] = time.perf_counter()
        else:
            deduped["signal_ts"] = pd.Series(dtype=float)
        return deduped

    def _deduplicate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        """Collapse consecutive same-type clusters into a single peak event.

        Two adjacent rows belong to the same cluster when they are consecutive
        in the *original* DataFrame positional index (no gap) and share the
        same signal label.  Within each cluster, the row with the largest
        absolute ``cvd_delta`` is selected as the representative signal.

        This prevents a single prolonged absorption episode — which may span
        dozens of 100 ms ticks — from producing dozens of near-identical rows
        in the output table.
        """
        if df.empty:
            return df

        original_positions: pd.Series = df.index.to_series()
        is_new_cluster: pd.Series = (
            (original_positions.diff().fillna(2) > 1)
            | df["signal"].ne(df["signal"].shift())
        )
        cluster_ids: pd.Series = is_new_cluster.cumsum()

        peak_indices: pd.Series = (
            df.assign(_cluster=cluster_ids)
            .groupby("_cluster")["cvd_delta"]
            .apply(lambda s: s.abs().idxmax())
        )
        return df.loc[peak_indices.to_numpy()]
