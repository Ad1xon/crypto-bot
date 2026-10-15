# Crypto-Quant

A modular, asynchronous algorithmic trading system for **Hyperliquid L1
perpetual futures**. Signal generation uses Smart Money Concept microstructure
analysis (CVD/OFI absorption) gated by Deribit Gamma Exposure, macro event
blackout windows, and FinBERT sentiment scoring.

Paper-trading by default. Live mode is a single env-var flip.

---

## Architecture

```mermaid
flowchart TD
    subgraph Data Ingestion
        HL[Hyperliquid WebSocket] -->|L2Book 20-level, Trades| Streamer(HyperliquidStreamer)
        Streamer -->|BBO, CVD, OFI| Logger(TickLogger)
        Logger -->|Parquet| Storage[(Local Storage)]
        Deribit[Deribit REST] -->|GEX snapshot| GEXFile[(data/live_gex.json)]
    end

    subgraph Signal Pipeline
        Streamer -->|tick| Engine(LiveSignalEngine)
        Engine -->|CVD + OFI| Detector(MicrostructureDetector)
        Engine -->|price, VWAP| Confluence(SuperConfluenceFilter)
        GEXFile -->|strike map| GEX(GexRiskManager)
        GEX --> Confluence
        Macro(MacroCalendarFilter) -->|blackout gate| Engine
        Sentiment(CryptoSentimentAnalyzer) -->|net_score| Sizer(PositionSizer)
        Confluence -->|verdict| Engine
    end

    subgraph Execution
        Engine -->|valid signal| Broker(CCXTBroker / PaperTrader)
        Broker -->|signed order| HL_Exec[Hyperliquid Exchange]
        Broker -.->|alert| Discord(Discord Webhook)
        Broker --> SigLog(SignalLogger)
    end

    subgraph Visualizer
        Streamer -.->|book updates| CLI(Rich CLIDashboard)
        Macro -.->|macro status| CLI
        Sentiment -.->|FinBERT score| CLI
    end

    classDef main fill:#1e1e2e,stroke:#89b4fa,stroke-width:2px,color:#cdd6f4;
    classDef storage fill:#313244,stroke:#a6e3a1,stroke-width:2px,color:#cdd6f4;
    classDef ext fill:#11111b,stroke:#f38ba8,stroke-width:2px,color:#cdd6f4;

    class Streamer,Engine,Detector,Confluence,GEX,Broker,Logger,CLI,Sizer,Macro,Sentiment,SigLog main;
    class Storage,GEXFile storage;
    class HL,HL_Exec,Discord,Deribit ext;
```

For a detailed breakdown of every module and the rationale behind each design
decision, see [ARCHITECTURE.md](./ARCHITECTURE.md).

---

## Stack

| Layer          | Technology                                          |
|----------------|-----------------------------------------------------|
| Language       | Python 3.10+                                        |
| Exchange       | Hyperliquid L1 (WebSocket + REST via CCXT)          |
| Options data   | Deribit REST (GEX snapshots)                        |
| Async          | `asyncio`                                           |
| Sentiment NLP  | ProsusAI/FinBERT (`transformers`)                   |
| Data storage   | Apache Parquet (`pyarrow`)                          |
| Terminal UI    | `rich`                                              |
| Linting        | `ruff`                                              |
| Testing        | `pytest` + `pytest-asyncio`                         |
| CI/CD          | GitHub Actions                                      |

---

## Quickstart

```bash
# 1. Clone and create virtual environment
git clone https://github.com/Ad1xon/crypto-bot.git
cd crypto-bot
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux / macOS

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure
cp .env.example .env
# Edit .env — set TRADING_SYMBOL, USE_TESTNET, etc.

# 4. Run (paper trading by default)
python main.py
```

---

## Development

```bash
# Install pre-commit hooks (enforces ruff before every commit)
pip install pre-commit
pre-commit install

# Run tests
pytest

# Lint + format
ruff check . --fix
ruff format .
```

---

## Instruments

| Symbol | Exchange    | Type      | Status  |
|--------|-------------|-----------|---------|
| `BTC`  | Hyperliquid | Perpetual | Active  |
| `ETH`  | Hyperliquid | Perpetual | Planned |
| `SOL`  | Hyperliquid | Perpetual | Planned |

---

## Status

Paper trading. Live execution via `LIVE_MODE=True` targets Hyperliquid testnet
by default (`USE_TESTNET=True`). Flip both flags only after validating the
backtest Profit Factor on historical tick data.
