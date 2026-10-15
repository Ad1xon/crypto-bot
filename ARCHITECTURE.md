# Architecture — Crypto-Quant

A modular, asynchronous algorithmic trading system targeting **Hyperliquid L1
perpetual futures** (BTC, ETH, SOL). Signal generation is driven by
microstructure analysis (Smart Money Concepts) combined with options market
data (Gamma Exposure from Deribit), macro event filtering, and NLP sentiment
scoring.

---

## High-Level Data Flow

```
  Deribit REST              Hyperliquid WebSocket
  (GEX snapshot)            (L2 Book + Trades)
        |                          |
        v                          v
  data/live_gex.json     HyperliquidStreamer
  (disk, 30 s TTL)               |
        |                         |  BBO, CVD, OFI
        |                         v
        |               LiveSignalEngine
        |                    |        |
        |          +---------+        +--------------+
        |          |                                  |
        v          v                                  v
   GexRiskManager  MicrostructureDetector      PositionSizer
   SuperConfluence  (BULLISH / BEARISH         (Half-Kelly +
   (VWAP + GEX)     ABSORPTION)                gex_modifier)
        |                  |
        +------+-----------+
               |  Signal verdict
               v
         MacroCalendarFilter -- blocked? -> drop signal
               |
               v
       CCXTBroker / PaperTrader
       (Post-Only limit -> fill)
               |
     +---------+----------+
     v                    v
 SignalLogger       DiscordAlerter
 (Parquet + log)   (webhook alert)
               |
               v
         CLIDashboard
     (Rich terminal UI)
      + background tasks:
        MacroCalendarFilter
        CryptoSentimentAnalyzer
```

---

## Module Reference

### `config/settings.py`

Single source of configuration truth. All config objects are **frozen
dataclasses** loaded once at process start from the `.env` file.

`AppConfig` composes:
- `HyperliquidWSConfig` — WebSocket URL (testnet/mainnet) and symbol
- `ReconnectPolicy` — exponential back-off parameters
- `TickLoggerConfig` — Parquet flush tuning
- `DeribitConfig` — REST base URL
- `AlertingConfig` — Discord webhook URL
- `BrokerConfig` — credentials (constructed lazily via `build_broker()` so
  paper-trading processes never require live API keys)

**Decision**: Lazy `BrokerConfig` construction means a missing API key raises a
`RuntimeError` at the point of first use, not at import time, allowing
paper-trading and backtesting without credentials.

---

### `data_ingestion/`

**`HyperliquidStreamer`** — a single persistent WebSocket connection to
Hyperliquid. Subscribes to two channels:

- `l2Book` — full 20-level depth snapshot on every update
- `trades` — individual trade events for CVD calculation

It dispatches events to `LiveSignalEngine` and `TickLogger`. It makes **no
trading decisions**.

**`TickLogger`** — non-blocking async writer. Buffers BBO and trade events in
memory queues and flushes them as Parquet files on a configurable interval.
Designed to never slow down the WebSocket receive loop.

**Decision**: Separating I/O (streamer) from persistence (tick logger) keeps
each class focused and testable independently.

---

### `quant_models/`

The analytical core. No module here ever calls an exchange API.

**`MicrostructureDetector`** — vectorised pandas signal detector.
Classifies each tick as `BULLISH_ABSORPTION`, `BEARISH_ABSORPTION`, or `NONE`
based on three simultaneous conditions:
1. CVD diverges significantly from price (z-score threshold)
2. OFI smoothed confirms passive limit-order flow on the opposing side
3. Price holds despite directional CVD pressure

**`SuperConfluenceFilter`** — the **canonical entry gate**, shared by both the
live engine and the event backtester (DRY). Evaluates:
- VWAP side alignment (price relative to VWAP)
- Nearest-strike GEX sign alignment (dealer gamma regime)

**`GexRiskManager`** — loads the Deribit GEX JSON snapshot from disk. Rejects
stale data (> 30 s old) with a `CRITICAL` log. Exposes `evaluate_signal()` for
direct GEX-only checks and `get_gex_map()` for passing the full map to
`SuperConfluenceFilter`.

**`PositionSizer`** — Half-Kelly criterion sizing, hard-capped at 5% of
account equity. Accepts a `gex_modifier` (0.0-1.0) to scale down position size
in adverse gamma regimes.

**`MacroCalendarFilter`** — fetches this week's USD high-impact events from the
ForexFactory JSON feed. Blocks trading +/-15 minutes around each event.

**`CryptoSentimentAnalyzer`** — fetches RSS feeds (CoinTelegraph, CoinDesk)
and classifies headlines with **ProsusAI/FinBERT** (lazily loaded). Returns a
weighted `net_score` suitable for use as a `gex_modifier` input.

---

### `execution/`

**`LiveSignalEngine`** — the central decision loop. Receives tick events from
`HyperliquidStreamer`, runs `MicrostructureDetector`, validates through
`SuperConfluenceFilter`, and routes signals to the configured execution engine.

**`PaperTrader`** — an `ExecutionEngine` implementation that simulates fills
without touching the exchange. State is persisted to disk between restarts.

**`CCXTBroker`** — CCXT-backed live execution engine for Hyperliquid. Default
order type is Post-Only limit. Supports testnet via `USE_TESTNET=True`.

**`SignalLogger`** — appends structured signal events (entry, exit, PnL) to
both a Parquet log and `app.log`.

**`DiscordAlerter`** — sends webhook payloads on fills, circuit-breaker
triggers, and connectivity events.

---

### `research/`

**`event_backtester.py`** — a pure tick-level event-driven backtest engine.
Processes ticks in chronological order, checks exits before entries on the same
tick to avoid look-ahead bias, and applies the same `SuperConfluenceFilter`
used in production. Returns a `PerformanceReport` with Profit Factor, max
drawdown, Sharpe, and per-trade records.

---

### `visualizer/`

**`CLIDashboard`** — a Rich-powered full-screen terminal UI that renders the
live L2 orderbook (20 levels), BBO, Spread, OFI, CVD, and — via background
async tasks — the current macro blackout status and FinBERT sentiment score.

---

## Key Architectural Decisions

### 1. Hyperliquid over Centralised Exchanges
Bybit and Binance were rejected due to European KYC restrictions and inferior
API latency profiles. Hyperliquid L1 offers permissionless on-chain perpetuals
with a WebSocket feed from their Tokyo-hosted matching engine.

> **Latency note**: the matching engine runs in AWS `ap-northeast-1`. For
> latency-sensitive strategies, production deployment should target a Tokyo VPS
> (AWS/Vultr/DigitalOcean), not a European data centre.

### 2. Deribit GEX as an External Signal
BTC options are predominantly traded on Deribit. Dealer gamma exposure (GEX) at
each strike determines the "gravitational pull" levels where market makers hedge
their delta. The system uses this as a regime filter, not as a primary signal.
A negative GEX environment (dealers are short gamma) means explosive moves are
more likely — entry criteria are tightened in that regime.

### 3. SuperConfluenceFilter as the Shared Predicate (DRY)
The entry gate runs in both live (`live_engine.py`) and backtest
(`event_backtester.py`) environments. Having a single implementation in
`quant_models/super_confluence.py` ensures that backtest results faithfully
represent what the live system would do — no parameter drift between research
and production.

### 4. Half-Kelly Sizing with Hard Caps
Pure Kelly maximises long-run expected log-wealth but produces drawdowns
unacceptable in practice. Half-Kelly halves the fraction, trading some expected
growth for significantly lower variance. The additional 5% equity hard cap
prevents runaway sizing during winning streaks when the Kelly fraction may
temporarily recommend an oversized position.

### 5. Event-Driven Backtest (No Vector Look-Ahead)
The backtester processes ticks one at a time in a `for` loop, mirroring the
live event loop. Exit conditions are checked before entry conditions on each
tick. This eliminates the most common source of backtest optimism (look-ahead
bias) where vectorised approaches accidentally use future data.

### 6. Frozen Dataclass Config (No Mutable Global State)
All configuration objects are `frozen=True` dataclasses. This prevents accidental
mutation of config at runtime and makes config objects trivially serialisable
and testable.

### 7. Paper Trading by Default
`LIVE_MODE=False` in `.env`. The paper trader (`PaperTrader`) implements the
same `ExecutionEngine` interface as `CCXTBroker`, so switching to live is a
single environment variable flip — no code changes required.

### 8. CI/CD Pipeline
GitHub Actions runs two independent jobs:
- **`quality`**: `ruff format --check .` then `ruff check .`
- **`test`**: `pytest` with asyncio mode

`.pre-commit-config.yaml` enforces the same checks locally before commits
reach the remote.

---

## Instruments

| Symbol | Exchange    | Type      | Status  |
|--------|-------------|-----------|---------|
| `BTC`  | Hyperliquid | Perpetual | Active  |
| `ETH`  | Hyperliquid | Perpetual | Planned |
| `SOL`  | Hyperliquid | Perpetual | Planned |

GEX data source: Deribit (BTC and ETH options, USD-settled).

---

## Deployment Target (Planned)

| Component     | Target                                                  |
|---------------|---------------------------------------------------------|
| Runtime       | Docker container                                        |
| VPS region    | Tokyo (AWS/Vultr) — co-located with HL matching engine  |
| Orchestration | `systemd` or Docker Compose                             |
| Secrets       | `.env` mounted as volume                                |
| Monitoring    | Discord alerts + `app.log`                              |
