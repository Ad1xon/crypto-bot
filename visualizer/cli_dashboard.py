"""High-performance Rich terminal dashboard for real-time L2 order book data.

Renders a full-screen live layout composed of a summary header panel and a
side-by-side bid/ask depth table.  The header displays symbol, spread, OFI,
and Cumulative Volume Delta (CVD).  Designed to be driven at WebSocket tick
rate without flickering — Rich's Live context batches redraws to a
configurable refresh cap.
"""

from __future__ import annotations

from typing import Final

from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

_BOOK_DEPTH: Final[int] = 10
_REFRESH_PER_SEC: Final[int] = 15


class CLIDashboard:
    """Rich-powered live terminal dashboard for L2 order book visualisation.

    Responsibilities:
    - Own and manage the Rich Live context lifecycle.
    - Accept a stream of (bids, asks, cvd) snapshots and re-render on every call.
    - Calculate and display spread, OFI, and CVD directly from live market data.
    """

    def __init__(self, symbol: str) -> None:
        self._symbol: str = symbol.upper()
        self._console: Console = Console()
        self._live: Live = Live(
            console=self._console,
            refresh_per_second=_REFRESH_PER_SEC,
            screen=True,
        )

    def start(self) -> None:
        """Enter the Rich Live full-screen context."""
        self._live.start(refresh=False)

    def stop(self) -> None:
        """Exit the Rich Live context and restore the terminal."""
        self._live.stop()

    def update(
        self,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
        cvd: float = 0.0,
    ) -> None:
        """Push a new order book snapshot and current CVD into the live display."""
        self._live.update(self._build_layout(bids, asks, cvd))

    def _build_layout(
        self,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
        cvd: float,
    ) -> Layout:
        """Assemble the full terminal layout from current market data."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=6),
            Layout(name="body"),
        )
        layout["header"].update(self._build_header(bids, asks, cvd))
        layout["body"].update(self._build_body(bids, asks))
        return layout

    def _build_header(
        self,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
        cvd: float,
    ) -> Panel:
        """Construct the summary panel with symbol, spread, OFI, CVD, and NBBO."""
        best_bid_price: float = bids[0][0] if bids else 0.0
        best_ask_price: float = asks[0][0] if asks else 0.0
        best_bid_vol: float = bids[0][1] if bids else 0.0
        best_ask_vol: float = asks[0][1] if asks else 0.0

        spread: float = best_ask_price - best_bid_price
        ofi: float = best_bid_vol - best_ask_vol

        ofi_color: str = "green" if ofi >= 0 else "red"
        ofi_sign: str = "+" if ofi >= 0 else ""

        cvd_color: str = "bold green" if cvd >= 0 else "bold red"
        cvd_sign: str = "+" if cvd >= 0 else ""

        lines = (
            f"\n  [bold cyan]{self._symbol}[/bold cyan]"
            f"     [dim]Spread:[/dim] [yellow]{spread:,.2f}[/yellow]"
            f"     [dim]OFI:[/dim] [{ofi_color}]{ofi_sign}{ofi:.6f}[/{ofi_color}]"
            f"     [dim]CVD:[/dim] [{cvd_color}]{cvd_sign}{cvd:,.4f}[/{cvd_color}]"
            f"\n  [dim]Best Bid:[/dim] [bold green]{best_bid_price:,.2f}[/bold green]"
            f"     [dim]Best Ask:[/dim] [bold red]{best_ask_price:,.2f}[/bold red]"
        )
        return Panel(
            lines,
            title="[bold bright_white]V3-Crypto-Quant  |  Live Order Book[/bold bright_white]",
            border_style="bright_blue",
            padding=(0, 1),
        )

    def _build_body(
        self,
        bids: list[tuple[float, float]],
        asks: list[tuple[float, float]],
    ) -> Columns:
        """Render the side-by-side bid and ask depth tables."""
        return Columns(
            [self._build_bid_table(bids), self._build_ask_table(asks)],
            equal=True,
            expand=True,
        )

    def _build_bid_table(self, bids: list[tuple[float, float]]) -> Table:
        """Build the green bids table for the top N price levels."""
        table = Table(
            title="[bold green] BIDS [/bold green]",
            border_style="green",
            header_style="bold green",
            show_lines=False,
            expand=True,
        )
        table.add_column("Price (USDT)", style="green", justify="right")
        table.add_column("Volume (BTC)", style="bright_green", justify="right")

        for price, volume in bids[:_BOOK_DEPTH]:
            table.add_row(f"{price:>14,.2f}", f"{volume:>14.6f}")

        return table

    def _build_ask_table(self, asks: list[tuple[float, float]]) -> Table:
        """Build the red asks table for the top N price levels."""
        table = Table(
            title="[bold red] ASKS [/bold red]",
            border_style="red",
            header_style="bold red",
            show_lines=False,
            expand=True,
        )
        table.add_column("Price (USDT)", style="red", justify="right")
        table.add_column("Volume (BTC)", style="bright_red", justify="right")

        for price, volume in asks[:_BOOK_DEPTH]:
            table.add_row(f"{price:>14,.2f}", f"{volume:>14.6f}")

        return table
