# ADR-0002: Prometheus + Grafana + Alertmanager for observability

- **Status:** Accepted
- **Date:** 2026-10-29

## Context

Before this change the only signals were a rotating log file and ad-hoc Discord
webhooks from inside `CCXTBroker`. A silently stalled WebSocket (process alive,
no data) was undetectable, and there was no history of PnL, signal rates or
resource usage.

## Decision

- The bot and the GEX collector expose **Prometheus** metrics via
  `prometheus_client` on `:9100` / `:9101` (`crypto_quant.observability.metrics`).
- **Prometheus** scrapes them plus **node-exporter** (host) and **cAdvisor** (containers).
- **Alertmanager** routes alerts to Discord, with inhibition rules to avoid duplicate pages.
- **Grafana** dashboards and datasources are provisioned from git (dashboards-as-code).
- The Docker `HEALTHCHECK` reads the same metrics endpoint and fails when the
  WebSocket heartbeat gauge is older than 120 s - liveness means *data flows*.

## Metric design

- Namespace `crypto_quant_`, base units (`_seconds`, `_usdt`, `_btc`), `_total` for counters.
- Bounded label cardinality: labels come from closed sets (channel, verdict, reason,
  engine, side, outcome). Never prices, order ids or timestamps.
- Heartbeats are exported as Unix timestamps (`*_timestamp_seconds`) so staleness is
  computed in PromQL (`time() - x`) and survives scrape gaps.
- Exporter failures are logged and swallowed - losing observability must never stop trading.

## Alerting philosophy

Page (critical) on symptoms that cost money or data: bot down, feed stale,
kill-switch tripped, order errors, OOM. Warn on leading indicators: reconnect
flapping, GEX staleness, drawdown at 50% of limit, disk < 15%. Every alert has a
`runbook_url` pointing to [`docs/RUNBOOK.md`](../RUNBOOK.md).

## Alternatives considered

- **Grafana Cloud free tier** - less to operate, but ships trading telemetry to a third party.
- **Loki + Promtail for logs** - logs are already JSON on stdout (`LOG_FORMAT=json`),
  so Loki can be added later without code changes. Deferred to save ~300 MB RAM on a 4 GB host.
- **OpenTelemetry** - better for distributed tracing across many services; overkill for two processes.

## Consequences

- ~400 MB extra RAM for the monitoring profile; fits CX22 (4 GB) with swap.
- Monitoring runs on the same host it monitors: a full host outage would not alert.
  Mitigation path: external uptime probe (e.g. Healthchecks.io dead-man switch).
