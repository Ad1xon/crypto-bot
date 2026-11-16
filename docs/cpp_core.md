# crypto_core (C++ order book)

`crypto_core` is a small C++17 extension (pybind11) that keeps the Hyperliquid
L2 book in memory and updates CVD and OFI on every message.
`HyperliquidStreamer` uses it through `data_ingestion/order_book.py`. If the
extension isn't installed, it falls back to a pure-Python book with the same
interface.

## Why

Every `l2Book` message has 20 bid and 20 ask levels with prices and sizes as
strings. The old handler turned all of them into Python tuples on the event
loop (80 `float()` calls per message) just to read the top of the book. The C++
version parses the message directly into fixed-size arrays and also computes
OFI for all 20 levels, which would be too slow to do in Python on every update.

## Files

| File | |
|---|---|
| `src/cpp_core/orderbook.hpp/.cpp` | the order book, plain C++ (no Python headers) |
| `src/cpp_core/bindings.cpp` | pybind11 wrapper |
| `src/cpp_core/crypto_core.pyi` | type stubs |
| `src/cpp_core/CMakeLists.txt` | build, pulls pybind11 with FetchContent |
| `src/crypto_quant/data_ingestion/order_book.py` | picks C++ or the Python fallback |
| `benchmarks/bench_orderbook.py` | records l2Book messages and benchmarks both versions |

## How it works

- Each side is a `std::array<Level, 20>`, so updates never allocate.
- Every side has two buffers. A new snapshot goes into the unused one and the
  index is flipped, so the previous book is still there for OFI without copying.
- **OFI** per level `m`, between the previous and the current snapshot:

  ```
  e_m = 1{Pb >= Pb_prev} * qb - 1{Pb <= Pb_prev} * qb_prev
      - 1{Pa <= Pa_prev} * qa + 1{Pa >= Pa_prev} * qa_prev
  ```

  Positive means buying pressure (bids added, or asks pulled). A missing level
  counts as price 0 (bid) or +inf (ask) with size 0.
- **CVD** = taker buy volume − taker sell volume. It resets on the first trade
  of a new UTC day.

The streamer still passes `top_imbalance` (best bid size − best ask size) to the
signal engine as `ofi`, the same value as before, so signals don't change. Using
the real multi-level OFI in `MicrostructureDetector` needs a backtest first.

## Build and test

Requires CMake ≥ 3.18, g++/clang and Python ≥ 3.11 (on Windows use WSL or
Docker, or Visual Studio Build Tools).

```bash
cmake -S src/cpp_core -B build/cpp_core -DCMAKE_BUILD_TYPE=Release -DPython_EXECUTABLE="$(which python)"
cmake --build build/cpp_core -j
cmake --install build/cpp_core --prefix "$(python -c 'import sysconfig; print(sysconfig.get_path("platlib"))')"
pytest tests/test_cpp_core.py tests/test_order_book.py tests/test_hyperliquid_streamer.py
```

Without the extension, the C++ tests are skipped and the rest run against the
Python fallback. The Docker image (builder stage) and the CI `test` job build the
extension automatically.

## Benchmark

```bash
python benchmarks/bench_orderbook.py record --coins BTC ETH SOL --seconds 120
python benchmarks/bench_orderbook.py run data/l2_recordings/l2book.jsonl
```

`record` saves raw `l2Book` messages from Hyperliquid mainnet (public, no API
keys needed). `run` replays them through the old `_handle_orderbook` parsing,
the Python fallback and `crypto_core`. It also checks that all three give the
same best bid/ask for every message.

Results on 288 messages (18 coins, ~3 min of mainnet data), 20 passes, Python
3.11 in Docker (`python:3.11-slim-bookworm`) on a desktop x86_64 CPU:

| implementation | mean [us] | p50 [us] | p99 [us] | speedup vs legacy |
|---|---:|---:|---:|---:|
| legacy `_handle_orderbook` parsing | 9.90 | 8.91 | 23.86 | 1.0x |
| Python fallback (`PyOrderBook`) | 12.11 | 11.20 | 25.37 | 0.8x |
| `crypto_core` | 4.10 | 3.91 | 7.26 | 2.4x |

BBO mismatches between the three: 0.

- About 2.4x faster on average and about 3x lower p99, while also computing OFI
  on all 20 levels (the Python versions don't).
- Most of the remaining ~4 us is reading the Python dicts and strings that
  `json.loads` creates. The book update itself is a small part of it, so parsing
  the raw JSON bytes in C++ would be the next step if this ever becomes a
  bottleneck.
- The Python fallback is a bit slower than the old code because it skips
  invalid levels (NaN, size 0), the same way the C++ version does.
