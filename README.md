# Crypto-Quant 📈

An advanced, asynchronous algorithmic trading system tailored for high-frequency data ingestion and order execution. Recently migrated to **Hyperliquid DEX** for zero gas fees, low latency, and no geo-blocking restrictions.

## Current Architecture

Below is a high-level representation of the current architecture:

```mermaid
flowchart TD
    subgraph Data Ingestion
        HL[Hyperliquid L1 WebSocket] -->|L2Book, Trades| Streamer(HyperliquidStreamer)
        Streamer -->|Tick & BBO| Logger(TickLogger)
        Logger -->|Parquet| Storage[(Local Storage)]
    end

    subgraph Signal Pipeline
        Streamer -->|Tick CVD & OFI| Engine(LiveSignalEngine)
        Engine -->|SMC Absorption| Detector(MicrostructureDetector)
        Engine -->|Gamma Risk| GEX(GexRiskManager)
    end

    subgraph Execution
        Engine -->|Valid Signal| Broker(CCXTBroker)
        Broker -->|REST API| HL_Exec[Hyperliquid Exchange]
        Broker -.->|Alerts| Discord(Discord Webhook)
    end

    subgraph Interface
        Streamer -.->|Dashboard Updates| CLI(Rich CLIDashboard)
    end

    classDef main fill:#1e1e2e,stroke:#89b4fa,stroke-width:2px,color:#cdd6f4;
    classDef storage fill:#313244,stroke:#a6e3a1,stroke-width:2px,color:#cdd6f4;
    classDef ext fill:#11111b,stroke:#f38ba8,stroke-width:2px,color:#cdd6f4;

    class Streamer,Engine,Detector,GEX,Broker,Logger,CLI main;
    class Storage storage;
    class HL,HL_Exec,Discord ext;
```

## Features
- **Hyperliquid Integration**: Native asynchronous streams parsing L2 orderbooks and trades.
- **Smart Money Concepts (SMC)**: Microstructure detectors searching for real-time absorptions using Order Flow Imbalance (OFI) and Cumulative Volume Delta (CVD).
- **Gamma Exposure (GEX)**: Risk management layer that calibrates positions according to the derivatives market's gamma profile.
- **Async Execution**: `CCXTBroker` leverages `ccxt.async_support` for rapid, non-blocking order placement.

## Next Steps / Roadmap
1. **Fleshing out Hyperliquid Data Parse**: Adapting the multi-level L2Book snapshot diffs perfectly into our internal book state.
2. **Backtesting Pipeline**: Wiring historical data into the `LiveSignalEngine` via an offline simulator.
3. **Position Sizing**: Dynamically calculating Kelly fractions / risk based on account equity and current market GEX.
4. **Execution Logic**: Tracking real positions in `ccxt_broker` against Hyperliquid's REST API for trailing stops.
