"""Real-time BTC Gamma Exposure (GEX) visualizer.

Reads data/live_gex.json produced by data_ingestion/deribit_client.py and
renders a self-refreshing horizontal bar chart via matplotlib.animation.

Layout
------
- Y-axis: Strike prices filtered to ±PRICE_BAND_PCT of the current BTC spot.
- X-axis: Net GEX value (Call_OI·Γ - Put_OI·Γ) per strike.
- Green bars: Positive GEX — dealer long-gamma zone (price magnet).
- Red bars:   Negative GEX — dealer short-gamma zone (price repellant).

The animation interval is REFRESH_INTERVAL_MS (default 5 000 ms). The figure
is non-blocking: plt.show() returns immediately on most backends because
FuncAnimation drives its own event loop integration.

Usage (from project root)
--------------------------
    python visualizer/live_gex_plot.py
"""

from __future__ import annotations

import json
import pathlib
from typing import Final

import matplotlib.animation as animation
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker

DATA_FILE: Final[pathlib.Path] = pathlib.Path("data") / "live_gex.json"
REFRESH_INTERVAL_MS: Final[int] = 5_000
PRICE_BAND_PCT: Final[float] = 0.05

COLOR_POSITIVE: Final[str] = "#2ecc71"
COLOR_NEGATIVE: Final[str] = "#e74c3c"
COLOR_ZERO_LINE: Final[str] = "#888888"
COLOR_TITLE: Final[str] = "#e0e0e0"
COLOR_AXIS_LABEL: Final[str] = "#aaaaaa"
COLOR_TICK: Final[str] = "#aaaaaa"
COLOR_SPINE: Final[str] = "#333333"
BG_AXES: Final[str] = "#1a1a2e"
BG_FIGURE: Final[str] = "#0d0d1a"


def _load_snapshot() -> tuple[dict[str, float], float, str]:
    """Read and validate the latest GEX snapshot from disk.

    Returns
    -------
    gex_map:
        Strike string → net GEX float.
    spot:
        Current BTC spot price from the snapshot.
    timestamp:
        ISO-8601 timestamp string from the snapshot.

    Raises
    ------
    FileNotFoundError
        When the JSON file does not yet exist.
    json.JSONDecodeError
        When the file contains a partial or corrupt write.
    KeyError
        When expected top-level keys are absent.
    ValueError
        When a GEX value cannot be cast to float.
    """
    with open(DATA_FILE, "r", encoding="utf-8") as fh:
        payload: dict = json.load(fh)

    gex_raw: dict = payload["gex"]
    gex_map: dict[str, float] = {k: float(v) for k, v in gex_raw.items()}
    spot: float = float(payload.get("spot", 0.0))
    timestamp: str = payload.get("timestamp", "")
    return gex_map, spot, timestamp


def _filter_strikes(
    gex_map: dict[str, float],
    spot: float,
) -> tuple[list[float], list[float]]:
    """Return strikes and GEX values within ±PRICE_BAND_PCT of spot.

    When spot is unavailable (0.0) all strikes are returned unfiltered.
    Results are sorted ascending by strike for consistent chart ordering.
    """
    all_strikes = sorted(float(k) for k in gex_map.keys())

    if spot > 0.0:
        low = spot * (1.0 - PRICE_BAND_PCT)
        high = spot * (1.0 + PRICE_BAND_PCT)
        filtered = [s for s in all_strikes if low <= s <= high]
    else:
        filtered = all_strikes

    gex_vals = [gex_map[str(int(s))] for s in filtered]
    return filtered, gex_vals


def _style_axes(ax: plt.Axes, fig: plt.Figure) -> None:
    """Apply dark-theme styling to axes and figure background."""
    ax.set_facecolor(BG_AXES)
    fig.patch.set_facecolor(BG_FIGURE)
    ax.tick_params(colors=COLOR_TICK, labelsize=8)
    ax.xaxis.label.set_color(COLOR_AXIS_LABEL)
    ax.yaxis.label.set_color(COLOR_AXIS_LABEL)
    for spine in ax.spines.values():
        spine.set_edgecolor(COLOR_SPINE)


def _draw_frame(
    ax: plt.Axes,
    fig: plt.Figure,
    strikes: list[float],
    gex_vals: list[float],
    spot: float,
    timestamp: str,
) -> None:
    """Render one complete frame of the GEX bar chart."""
    colors = [COLOR_POSITIVE if g >= 0.0 else COLOR_NEGATIVE for g in gex_vals]
    y_pos = list(range(len(strikes)))

    bars = ax.barh(y_pos, gex_vals, color=colors, edgecolor="none", height=0.65)
    ax.bar_label(bars, fmt='%.3f', padding=5, color='#e0e0e0', fontsize=9, fontweight='bold')
    ax.axvline(0.0, color=COLOR_ZERO_LINE, linewidth=0.9, linestyle="--", zorder=3)

    ax.set_yticks(y_pos)
    ax.set_yticklabels([f"${int(s):,}" for s in strikes], fontsize=8)

    ax.xaxis.set_major_formatter(
        mticker.FuncFormatter(lambda val, _: f"{val:.3f}")
    )

    ts_display = timestamp[:19].replace("T", " ") if timestamp else "n/a"
    spot_display = f"${spot:,.0f}" if spot > 0.0 else "n/a"
    ax.set_title(
        f"BTC GEX Profile   ·   Spot ≈ {spot_display}   ·   {ts_display} UTC",
        fontsize=11,
        color=COLOR_TITLE,
        pad=12,
        fontweight="bold",
    )
    ax.set_xlabel(
        "Net GEX  =  (Call OI · Γ)  −  (Put OI · Γ)",
        fontsize=9,
        color=COLOR_AXIS_LABEL,
        labelpad=8,
    )
    ax.set_ylabel("Strike Price", fontsize=9, color=COLOR_AXIS_LABEL, labelpad=8)

    positive_count = sum(1 for g in gex_vals if g >= 0.0)
    negative_count = len(gex_vals) - positive_count
    ax.text(
        0.99, 0.01,
        f"▲ {positive_count} positive   ▼ {negative_count} negative",
        transform=ax.transAxes,
        fontsize=7,
        color=COLOR_AXIS_LABEL,
        ha="right",
        va="bottom",
    )

    _style_axes(ax, fig)


def animate(frame: int, ax: plt.Axes, fig: plt.Figure) -> None:
    """FuncAnimation callback — reload data and redraw chart each tick."""
    ax.clear()
    _style_axes(ax, fig)

    try:
        gex_map, spot, timestamp = _load_snapshot()
    except FileNotFoundError:
        ax.set_title(
            f"Waiting for {DATA_FILE} ...",
            fontsize=11,
            color=COLOR_AXIS_LABEL,
        )
        return
    except json.JSONDecodeError as exc:
        ax.set_title(
            f"JSON decode error — retrying  ({exc})",
            fontsize=9,
            color=COLOR_NEGATIVE,
        )
        return
    except (KeyError, ValueError) as exc:
        ax.set_title(
            f"Schema error — retrying  ({exc})",
            fontsize=9,
            color=COLOR_NEGATIVE,
        )
        return
    except OSError as exc:
        ax.set_title(
            f"File locked by Windows — retrying...",
            fontsize=9,
            color=COLOR_NEGATIVE,
        )
        return

    if not gex_map:
        ax.set_title("GEX map is empty — waiting for data ...", fontsize=11, color=COLOR_AXIS_LABEL)
        return

    strikes, gex_vals = _filter_strikes(gex_map, spot)

    if not strikes:
        band_pct = int(PRICE_BAND_PCT * 100)
        ax.set_title(
            f"No strikes within ±{band_pct}% of spot ({spot:,.0f})",
            fontsize=11,
            color=COLOR_AXIS_LABEL,
        )
        return

    _draw_frame(ax, fig, strikes, gex_vals, spot, timestamp)


def main() -> None:
    """Initialise the figure and start the animation event loop."""
    plt.style.use("dark_background")

    fig, ax = plt.subplots(figsize=(13, 11))
    fig.patch.set_facecolor(BG_FIGURE)
    fig.canvas.manager.set_window_title("BTC Live GEX Profile")  # type: ignore[union-attr]

    fig.ani = animation.FuncAnimation(
        fig,
        animate,
        fargs=(ax, fig),
        interval=REFRESH_INTERVAL_MS,
        cache_frame_data=False,
    )

    plt.tight_layout(pad=1.8)
    plt.show()


if __name__ == "__main__":
    main()
