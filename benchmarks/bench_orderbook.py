"""Benchmark the order book implementations on recorded Hyperliquid l2Book messages.

Compares, per message, the work the streamer does before metrics/logging:
  * legacy      - the parsing from the old HyperliquidStreamer._handle_orderbook
  * python      - PyOrderBook (pure-Python fallback)
  * crypto_core - C++ OrderBook (also computes OFI on all 20 levels)

Usage:
    python benchmarks/bench_orderbook.py record --coins BTC ETH SOL --seconds 120
    python benchmarks/bench_orderbook.py run data/l2_recordings/l2book.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import pathlib
import platform
import statistics
import time
from collections.abc import Callable
from typing import Any, Final

from crypto_quant.data_ingestion.order_book import OrderBookLike, PyOrderBook

HL_WS_URL: Final[str] = "wss://api.hyperliquid.xyz/ws"
DEFAULT_OUT_DIR: Final[pathlib.Path] = pathlib.Path("data/l2_recordings")

Message = dict[str, Any]  # one l2Book "data" payload, as json.loads returns it
Bbo = tuple[float, float, float, float]
Handler = Callable[[Message], Bbo]


async def record(coins: list[str], seconds: float, out: pathlib.Path) -> int:
    import websockets

    out.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + seconds
    count = 0
    async with websockets.connect(HL_WS_URL) as ws:
        for coin in coins:
            await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}))
        with out.open("w", encoding="utf-8") as f:
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except TimeoutError:
                    break
                msg = json.loads(raw)
                if msg.get("channel") != "l2Book":
                    continue
                f.write(json.dumps(msg["data"]) + "\n")
                count += 1
    return count


def load_messages(path: pathlib.Path) -> list[Message]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def legacy_handler() -> Handler:
    """Copy of the parsing the streamer did before crypto_core."""

    def handle(data: Message) -> Bbo:
        levels = data.get("levels", [[], []])
        bids = [(float(lvl.get("px", 0)), float(lvl.get("sz", 0))) for lvl in levels[0][:20]]
        asks = [(float(lvl.get("px", 0)), float(lvl.get("sz", 0))) for lvl in levels[1][:20]] if len(levels) > 1 else []
        best_bid = bids[0] if bids else (0.0, 0.0)
        best_ask = asks[0] if asks else (0.0, 0.0)
        return best_bid[0], best_bid[1], best_ask[0], best_ask[1]

    return handle


def book_handler(make_book: Callable[[], OrderBookLike]) -> Handler:
    books: dict[str, OrderBookLike] = {}

    def handle(data: Message) -> Bbo:
        # One book per coin, otherwise OFI would compare e.g. a BTC snapshot with an ETH one.
        book = books.get(data["coin"])
        if book is None:
            book = books[data["coin"]] = make_book()
        book.update_hyperliquid(data["levels"], data["time"])
        _ = book.spread, book.mid_price, book.top_imbalance, book.cvd  # fields the streamer reads too
        return book.best_bid_price, book.best_bid_size, book.best_ask_price, book.best_ask_size

    return handle


def time_handler(make: Callable[[], Handler], messages: list[Message], repeats: int) -> list[int]:
    """Nanoseconds per message, over `repeats` passes (after one warm-up pass)."""
    handle = make()
    for msg in messages:
        handle(msg)
    samples: list[int] = []
    for _ in range(repeats):
        handle = make()
        for msg in messages:
            t0 = time.perf_counter_ns()
            handle(msg)
            samples.append(time.perf_counter_ns() - t0)
    return samples


def count_mismatches(handlers: dict[str, Handler], messages: list[Message]) -> dict[str, int]:
    """How many messages give a different BBO than the legacy handler."""
    reference = handlers["legacy"]
    mismatches = {name: 0 for name in handlers if name != "legacy"}
    for msg in messages:
        expected = reference(msg)
        for name in mismatches:
            if handlers[name](msg) != expected:
                mismatches[name] += 1
    return mismatches


def percentile(samples: list[int], q: float) -> float:
    ordered = sorted(samples)
    return float(ordered[min(len(ordered) - 1, int(q * len(ordered)))])


def run(path: pathlib.Path, repeats: int) -> None:
    messages = load_messages(path)
    if not messages:
        raise SystemExit(f"{path} has no messages")

    makers: dict[str, Callable[[], Handler]] = {
        "legacy": legacy_handler,
        "python": lambda: book_handler(PyOrderBook),
    }
    try:
        import crypto_core

        makers["crypto_core"] = lambda: book_handler(crypto_core.OrderBook)
    except ImportError:
        print("crypto_core not installed, skipping the C++ order book\n")

    mismatches = count_mismatches({name: make() for name, make in makers.items()}, messages)
    results = {name: time_handler(make, messages, repeats) for name, make in makers.items()}
    legacy_mean = statistics.fmean(results["legacy"])

    print(f"{len(messages)} messages from {path}, {repeats} passes")
    print(f"Python {platform.python_version()} on {platform.machine()} ({platform.system()})\n")
    print("| implementation | mean [us] | p50 [us] | p99 [us] | msgs/s | speedup | BBO mismatches |")
    print("|---|---:|---:|---:|---:|---:|---:|")
    for name, samples in results.items():
        mean = statistics.fmean(samples)
        print(
            f"| {name} | {mean / 1e3:.2f} | {percentile(samples, 0.5) / 1e3:.2f} "
            f"| {percentile(samples, 0.99) / 1e3:.2f} | {1e9 / mean:,.0f} "
            f"| {legacy_mean / mean:.1f}x | {mismatches.get(name, 0)} |"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("record", help="record l2Book messages from Hyperliquid mainnet (public, no keys)")
    rec.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL"])
    rec.add_argument("--seconds", type=float, default=120.0)
    rec.add_argument("--out", type=pathlib.Path, default=DEFAULT_OUT_DIR / "l2book.jsonl")

    bench = sub.add_parser("run", help="replay a recording through all order books")
    bench.add_argument("path", type=pathlib.Path)
    bench.add_argument("--repeats", type=int, default=20)

    args = parser.parse_args()
    if args.command == "record":
        out = args.out
        n = asyncio.run(record(args.coins, args.seconds, out))
        print(f"recorded {n} l2Book messages to {out}")
    else:
        run(args.path, args.repeats)


if __name__ == "__main__":
    main()
