"""Interactive microstructure dashboard for tick-level trade and BBO data.

Loads the latest Parquet batches produced by TickLogger, aligns them via
pd.merge_asof, and renders a three-row Plotly dashboard saved as a
self-contained HTML file that opens automatically in the default browser.

Panel layout
------------
Row 1  Price & Depth   — Taker Buy/Sell scatter + Best Bid/Ask dashed lines.
Row 2  CVD             — Cumulative Volume Delta step-line, filled to zero.
Row 3  OFI             — Order Flow Imbalance bar chart at 100 ms cadence.

All x-axes are shared so any zoom or pan synchronises every panel.
"""

from __future__ import annotations

import pathlib
import webbrowser
from typing import Final

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_DATA_DIR: Final[pathlib.Path] = pathlib.Path("data")
_OUTPUT_FILE: Final[pathlib.Path] = pathlib.Path("research") / "microstructure_dashboard.html"
_TEMPLATE: Final[str] = "plotly_dark"


def load_parquet_glob(data_dir: pathlib.Path, prefix: str) -> pd.DataFrame:
    """Concatenate every Parquet file whose name starts with *prefix*.

    Files are discovered with a glob, concatenated in chronological order
    (determined by the filename timestamp suffix), and the result is sorted
    by ``timestamp_ms`` with a clean integer index.

    Raises ``FileNotFoundError`` when no matching files exist.
    """
    files: list[pathlib.Path] = sorted(data_dir.glob(f"{prefix}_*.parquet"))
    if not files:
        raise FileNotFoundError(
            f"No Parquet files found in '{data_dir}' with prefix '{prefix}'"
        )
    return (
        pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
        .sort_values("timestamp_ms", kind="stable")
        .reset_index(drop=True)
    )


def build_trades_frame(data_dir: pathlib.Path) -> pd.DataFrame:
    """Load all trade Parquet files and attach CVD and datetime columns.

    CVD (Cumulative Volume Delta) is the running signed sum of executed
    quantity: ``+qty`` for taker buys (``is_buyer_maker=False``) and
    ``-qty`` for taker sells (``is_buyer_maker=True``).  The intermediate
    ``signed_qty`` column is dropped before returning.
    """
    df: pd.DataFrame = load_parquet_glob(data_dir, "trades")
    df["signed_qty"] = df["qty"].where(~df["is_buyer_maker"], -df["qty"])
    df["cvd"] = df["signed_qty"].cumsum()
    df["dt"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    return df.drop(columns=["signed_qty"])


def build_bbo_frame(data_dir: pathlib.Path) -> pd.DataFrame:
    """Load all BBO Parquet files, remove warmup artefacts, and add datetime.

    The very first depth messages arrive before the local order book is
    populated, producing ``best_ask_price == 0``.  Those rows are filtered
    out so they do not corrupt the price axis.
    """
    df: pd.DataFrame = load_parquet_glob(data_dir, "bbo")
    df = df[df["best_ask_price"] > 0].reset_index(drop=True)
    df["dt"] = pd.to_datetime(df["timestamp_ms"], unit="ms", utc=True)
    return df


def merge_frames(
    bbo_df: pd.DataFrame,
    trades_df: pd.DataFrame,
) -> pd.DataFrame:
    """Align trade ticks onto BBO snapshots with a backward asof merge.

    ``pd.merge_asof`` matches each BBO row (left, 100 ms cadence) with the
    most recent trade whose ``timestamp_ms`` is less than or equal to the
    BBO timestamp.  The resulting frame is the single enriched DataFrame
    that contains price, BBO, CVD, OFI, and spread at every 100 ms tick —
    useful for downstream ML feature engineering and backtesting.
    """
    left: pd.DataFrame = bbo_df.copy()
    right: pd.DataFrame = (
        trades_df[["timestamp_ms", "price", "cvd"]]
        .rename(columns={"price": "last_trade_price", "cvd": "last_cvd"})
    )
    return pd.merge_asof(
        left,
        right,
        on="timestamp_ms",
        direction="backward",
    )


def _ofi_colors(series: pd.Series) -> list[str]:
    """Map non-negative OFI values to green and negative values to red."""
    return [
        "rgba(0,210,90,0.75)" if v >= 0 else "rgba(220,55,55,0.75)"
        for v in series
    ]


def _add_price_depth_traces(
    fig: go.Figure,
    bbo_df: pd.DataFrame,
    trades_df: pd.DataFrame,
) -> None:
    """Populate Row 1 with Best Bid/Ask lines and Taker Buy/Sell markers.

    Best Bid and Best Ask are rendered as dotted lines to make the spread
    visible as a band.  Individual trade executions are plotted as
    up/down triangles coloured by aggressor side, enabling visual detection
    of spoofing (large quote walls that disappear before being hit).
    """
    fig.add_trace(
        go.Scatter(
            x=bbo_df["dt"],
            y=bbo_df["best_bid_price"],
            mode="lines",
            line=dict(dash="dot", color="rgba(0,210,90,0.55)", width=1),
            name="Best Bid",
            legendgroup="bbo",
            hovertemplate="Bid: %{y:,.2f}<extra></extra>",
        ),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=bbo_df["dt"],
            y=bbo_df["best_ask_price"],
            mode="lines",
            line=dict(dash="dot", color="rgba(220,60,60,0.55)", width=1),
            name="Best Ask",
            legendgroup="bbo",
            hovertemplate="Ask: %{y:,.2f}<extra></extra>",
        ),
        row=1, col=1,
    )

    buys: pd.DataFrame = trades_df[~trades_df["is_buyer_maker"]]
    sells: pd.DataFrame = trades_df[trades_df["is_buyer_maker"]]

    fig.add_trace(
        go.Scatter(
            x=buys["dt"],
            y=buys["price"],
            mode="markers",
            marker=dict(symbol="triangle-up", color="limegreen", size=5, opacity=0.9),
            name="Taker Buy",
            hovertemplate="Buy: %{y:,.2f}  qty=%{customdata:.5f}<extra></extra>",
            customdata=buys["qty"].values,
        ),
        row=1, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=sells["dt"],
            y=sells["price"],
            mode="markers",
            marker=dict(symbol="triangle-down", color="tomato", size=5, opacity=0.9),
            name="Taker Sell",
            hovertemplate="Sell: %{y:,.2f}  qty=%{customdata:.5f}<extra></extra>",
            customdata=sells["qty"].values,
        ),
        row=1, col=1,
    )


def _add_cvd_trace(fig: go.Figure, trades_df: pd.DataFrame) -> None:
    """Populate Row 2 with a step-line CVD filled to zero.

    Using ``line_shape='hv'`` renders a staircase that faithfully represents
    the discrete, event-driven nature of CVD — it changes only when a trade
    prints, not between ticks.  The semi-transparent fill makes divergences
    between price direction and CVD direction (Absorption patterns) easy to
    spot at a glance.
    """
    cvd: pd.Series = trades_df["cvd"]
    fig.add_trace(
        go.Scatter(
            x=trades_df["dt"],
            y=cvd,
            mode="lines",
            line=dict(shape="hv", color="rgba(80,185,255,0.90)", width=1.5),
            fill="tozeroy",
            fillcolor="rgba(80,185,255,0.08)",
            name="CVD",
            hovertemplate="CVD: %{y:,.4f}<extra></extra>",
        ),
        row=2, col=1,
    )
    fig.add_hline(
        y=0,
        line_dash="solid",
        line_color="rgba(255,255,255,0.15)",
        line_width=1,
        row=2, col=1,
    )


def _add_ofi_trace(fig: go.Figure, bbo_df: pd.DataFrame) -> None:
    """Populate Row 3 with a signed OFI bar chart at 100 ms cadence.

    Bars are coloured green for positive OFI (bid size > ask size, bullish
    pressure) and red for negative OFI (ask size > bid size, bearish
    pressure).  This panel is the most actionable for short-term flow
    traders as it shows instantaneous supply/demand imbalance at the quote.
    """
    fig.add_trace(
        go.Bar(
            x=bbo_df["dt"],
            y=bbo_df["ofi"],
            marker_color=_ofi_colors(bbo_df["ofi"]),
            name="OFI",
            hovertemplate="OFI: %{y:,.6f}<extra></extra>",
        ),
        row=3, col=1,
    )


def build_figure(
    bbo_df: pd.DataFrame,
    trades_df: pd.DataFrame,
) -> go.Figure:
    """Assemble and style the three-panel Plotly microstructure dashboard.

    All subplots share the same x-axis so zooming or panning in any panel
    simultaneously updates every other panel.  The hover mode is set to
    ``x unified`` so a single cursor position reveals all three metrics.
    """
    symbol: str = "BTCUSDT"
    time_start: str = bbo_df["dt"].min().strftime("%Y-%m-%d %H:%M:%S UTC")
    time_end: str = bbo_df["dt"].max().strftime("%Y-%m-%d %H:%M:%S UTC")

    fig: go.Figure = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        subplot_titles=(
            "Price & Depth  —  Best Bid / Ask  +  Execution Prints",
            "Cumulative Volume Delta  (CVD)",
            "Order Flow Imbalance  (OFI)  @  100 ms",
        ),
        row_heights=[0.45, 0.30, 0.25],
    )

    _add_price_depth_traces(fig, bbo_df, trades_df)
    _add_cvd_trace(fig, trades_df)
    _add_ofi_trace(fig, bbo_df)

    fig.update_layout(
        template=_TEMPLATE,
        title=dict(
            text=(
                f"V3-Crypto-Quant  |  Microstructure Dashboard  |  "
                f"{symbol}  |  {time_start} - {time_end}"
            ),
            font=dict(size=16, color="white"),
            x=0.01,
        ),
        height=1150,
        hovermode="x unified",
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.015,
            xanchor="right",
            x=1,
            font=dict(size=11),
        ),
        margin=dict(l=65, r=30, t=90, b=45),
        barmode="relative",
        paper_bgcolor="rgba(15,15,20,1)",
        plot_bgcolor="rgba(15,15,20,1)",
    )

    grid_color: str = "rgba(255,255,255,0.06)"
    fig.update_xaxes(showgrid=True, gridcolor=grid_color, zeroline=False)
    fig.update_yaxes(showgrid=True, gridcolor=grid_color, zeroline=False)

    fig.update_yaxes(title_text="Price (USDT)", row=1, col=1)
    fig.update_yaxes(title_text="CVD (BTC)", row=2, col=1)
    fig.update_yaxes(title_text="OFI (BTC)", row=3, col=1)
    fig.update_xaxes(title_text="UTC Time", row=3, col=1)

    for annotation in fig.layout.annotations:
        annotation.update(font=dict(size=13, color="rgba(200,200,200,0.85)"))

    return fig


def main() -> None:
    """Orchestrate data loading, merging, figure building, and browser launch."""
    print("Loading trade ticks ...")
    trades_df: pd.DataFrame = build_trades_frame(_DATA_DIR)

    print("Loading BBO snapshots ...")
    bbo_df: pd.DataFrame = build_bbo_frame(_DATA_DIR)

    print("Merging frames with asof alignment ...")
    merged_df: pd.DataFrame = merge_frames(bbo_df, trades_df)

    sep: str = "-" * 55
    print(
        f"\n{sep}\n"
        f"  Trades  : {len(trades_df):>8,} rows\n"
        f"  BBO     : {len(bbo_df):>8,} rows\n"
        f"  Merged  : {len(merged_df):>8,} rows\n"
        f"  Time    : {bbo_df['dt'].min().strftime('%H:%M:%S')} - "
        f"{bbo_df['dt'].max().strftime('%H:%M:%S')} UTC\n"
        f"  CVD     : {trades_df['cvd'].min():+.4f} to {trades_df['cvd'].max():+.4f} BTC\n"
        f"  Spread  : {bbo_df['spread'].median():.4f} USDT (median)\n"
        f"{sep}"
    )

    _OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    print("\nBuilding figure ...")
    fig: go.Figure = build_figure(bbo_df, trades_df)

    print(f"Writing HTML -> {_OUTPUT_FILE.resolve()}")
    fig.write_html(str(_OUTPUT_FILE), include_plotlyjs="cdn")

    print("Opening in browser ...")
    webbrowser.open(_OUTPUT_FILE.resolve().as_uri())


if __name__ == "__main__":
    main()
