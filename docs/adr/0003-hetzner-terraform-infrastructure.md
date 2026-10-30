# ADR-0003: Terraform-managed single Hetzner VM with a persistent data volume

- **Status:** Accepted
- **Date:** 2026-10-29

## Context

Data collection stopped every time the development PC slept, leaving gaps in the
tick and GEX history that invalidate backtests. A 24/7 host is required.

## Decision

- Provision on **Hetzner Cloud** with **Terraform** (`infra/terraform`):
  server, firewall, SSH key and a separate **block volume** for `data/`.
- First boot is configured with **cloud-init**: SSH hardening, Docker, swap,
  fail2ban, unattended upgrades, volume mount, repo checkout.
- The VM is disposable (cattle); the volume holding the dataset is protected (pet).
- No public web ports. Grafana/Prometheus bind to `127.0.0.1` and are reached via SSH tunnel.
- Secrets are never put in Terraform (state would contain them in plain text).

## Why Hetzner

~4 EUR/month for 2 vCPU / 4 GB vs ~30 USD on AWS for comparable on-demand capacity.
The trade-off is latency: Hyperliquid validators run in AWS Tokyo. From `fsn1` the
RTT is ~230 ms, from Hetzner `sin` (Singapore) ~70 ms. For the current phase (data
collection + paper trading) EU is acceptable; `location = "sin"` is a one-line change
before going live.

## src/ layout

Code moved to `src/crypto_quant/` (PyPA src layout) in the same change set:
tests run against the installed package rather than the working directory, imports
are unambiguous (`crypto_quant.config` instead of a top-level `config`), and the
Docker image installs a proper wheel with console entry points.
