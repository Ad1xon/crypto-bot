# Runbook

Short, actionable responses for every alert in
[`monitoring/prometheus/rules/alerts.yml`](../monitoring/prometheus/rules/alerts.yml).
All commands run on the host in `/opt/crypto-quant`.

```bash
alias dc='docker compose --env-file .env --env-file .compose.env --profile monitoring'
```

## BotDown

1. `dc ps` - is `bot` restarting, exited or unhealthy?
2. `dc logs --tail=200 bot` - look for a traceback or `RuntimeError` from config.
3. Config error: fix `.env`, then `dc up -d bot`.
4. Bad release: redeploy the previous tag via the *Deploy* workflow (`image_tag=sha-<previous>`).

## GexCollectorDown

1. `dc logs --tail=100 gex-collector`.
2. Restart: `dc restart gex-collector`. Trading is safe meanwhile - the confluence filter rejects signals without GEX.

## BotRestartLoop

Usually OOM or a crash on startup. Check `dmesg | grep -i oom` and `ContainerOomKilled`.
If memory: see [HostMemoryPressure](#hostmemorypressure).

## MarketDataStale

Process alive, no WebSocket messages for > 60 s.

1. `dc logs --tail=100 bot | grep -i websocket` - reconnect loop?
2. Check Hyperliquid status / `curl -sI https://api.hyperliquid.xyz/info`.
3. Host network: `ping -c3 api.hyperliquid.xyz`.
4. If the exchange is fine: `dc restart bot`. The Docker healthcheck will also mark the container unhealthy.

## WebSocketFlapping

Reconnects clear the rolling signal window and create tick-data gaps. Check host
network and Hyperliquid status. Persistent flapping from EU -> consider `location = "sin"`.

## GexSnapshotStale

`GexRiskManager` ignores snapshots older than 30 s, so the GEX gate is closed.
Check `GexFetchErrors` and the collector logs. Deribit 429 = rate limit; the collector backs off automatically.

## GexFetchErrors

`kind="http"` - Deribit 4xx/5xx, `kind="network"` - DNS/TCP, `kind="parse"` - API schema change (needs a code fix).

## KillSwitchActive

New entries are frozen. **Do not just restart.**

1. Inspect `data/paper_state.json` / `data/live_state.json` (`system_fault`, `session_pnl`).
2. Live mode: verify open orders/positions on the Hyperliquid UI and cancel orphans manually.
3. Understand *why* (logs, Grafana PnL panel) before resetting.
4. Reset by deleting the state file only after manual verification, then `dc restart bot`.

## SessionDrawdownWarning

50% of the daily loss limit consumed. Review recent trades in `data/signal_audit.jsonl`;
consider pausing (`dc stop bot`) during abnormal volatility.

## OrderLatencyHigh

p95 signal-to-ack > 500 ms. Check host CPU (FinBERT inference runs every 5 min),
network RTT to the exchange, and Hyperliquid API status.

## OrderErrors

The exchange rejected orders. Logs: `dc logs bot | grep "CCXT BROKER"`.
Three consecutive errors trip the circuit breaker (-> KillSwitchActive).

## HostDiskFillingUp

`df -h /mnt/data`. Tick data grows ~25 MB/day. Options: grow the volume
(`data_volume_size_gb` in Terraform + `resize2fs`), or archive old parquet files to object storage.

## HostMemoryPressure

`docker stats --no-stream`. The bot (torch + FinBERT) is the largest consumer.
Short term: `dc stop cadvisor`. Long term: `server_type = "cx32"` in Terraform.
