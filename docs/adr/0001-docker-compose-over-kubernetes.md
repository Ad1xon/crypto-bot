# ADR-0001: Docker Compose instead of Kubernetes

- **Status:** Accepted
- **Date:** 2026-10-29

## Context

The system is two long-running processes (trading bot, Deribit GEX collector)
plus an optional monitoring stack, deployed to a single Hetzner VM. We need
24/7 uptime, automatic restarts, reproducible deploys and observability.

Kubernetes was evaluated because it is the industry default for container
orchestration and would look strong on a CV.

## Decision

Use **Docker Compose** on a single host, provisioned by Terraform.

## Rationale

| Concern | What K8s gives | Does this project need it? |
|---|---|---|
| Horizontal scaling | ReplicaSets, HPA | **No - actively harmful.** The bot holds one WebSocket, one position and local state (`paper_state.json`, `live_state.json`). Two replicas = two concurrent order streams = double exposure. It must be a singleton. |
| Self-healing | Pod restarts, rescheduling across nodes | Restart: yes, Compose `restart: unless-stopped` + Docker healthcheck does it. Cross-node rescheduling: no, there is one node. |
| Rolling deploys | Zero-downtime rollout | **No.** A trading singleton must never overlap old/new versions; stop-then-start is the correct strategy. |
| Service discovery | DNS, Services | Compose network DNS (`bot:9100`) is sufficient for 7 containers. |
| Secrets | K8s Secrets | Compose file secrets cover the one secret (Discord webhook); `.env` stays off-image. |
| Cost | Managed control plane or 3-node HA cluster | One CX22 (~4 EUR/month) vs 3+ nodes. A control plane would use more RAM than the bot itself. |
| Ops burden | Upgrades, CNI, ingress, cert-manager, etcd backups | None of this produces trading edge. |

Latency also matters: an extra overlay network/kube-proxy hop adds jitter on
the 100 ms microstructure path for zero benefit.

## When to revisit

Move to **k3s** (single binary, same manifests as full K8s) if any of these become true:

- Multiple strategies/symbols run as independent bots (one Deployment each, `replicas: 1`, `strategy: Recreate`).
- A second region is added (e.g. Tokyo co-location next to Hyperliquid validators).
- Research jobs (param sweeps, ML training) need batch scheduling via `Job`/`CronJob`.

Migration cost is low because the image, healthcheck, metrics endpoint and
12-factor configuration are already Kubernetes-ready.

## Consequences

- Single point of failure at the host level, mitigated by: persistent Hetzner volume
  (data survives VM replacement), Terraform (VM is rebuilt in minutes), and
  `BotDown` / `MarketDataStale` alerts.
- No zero-downtime deploys - acceptable and intended for a stateful trading singleton.
