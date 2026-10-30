"""Container healthcheck that verifies data is flowing, not just that the process exists.

Fetches the local ``/metrics`` endpoint and fails when a heartbeat gauge (a Unix
timestamp) is older than ``--max-age`` seconds. Used by the Docker
``HEALTHCHECK`` so a bot whose WebSocket silently stalled is reported unhealthy.

Exit codes follow the Docker convention: 0 healthy, 1 unhealthy.
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request
from typing import Final

DEFAULT_METRIC: Final[str] = "crypto_quant_ws_last_message_timestamp_seconds"


def parse_gauge(metrics_text: str, metric: str) -> float | None:
    """Return the value of an unlabelled gauge from Prometheus text exposition."""
    for line in metrics_text.splitlines():
        if line.startswith("#"):
            continue
        name, _, value = line.partition(" ")
        if name == metric:
            try:
                return float(value.strip())
            except ValueError:
                return None
    return None


def evaluate(metrics_text: str, metric: str, max_age_s: float, now_s: float) -> tuple[bool, str]:
    """Decide health from the heartbeat gauge. A zero value means "never seen"."""
    ts = parse_gauge(metrics_text, metric)
    if ts is None:
        return False, f"metric {metric} not found"
    if ts <= 0:
        return False, f"{metric} has never been set"
    age = now_s - ts
    if age > max_age_s:
        return False, f"{metric} stale: {age:.0f}s > {max_age_s:.0f}s"
    return True, f"ok ({age:.1f}s old)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=9100)
    parser.add_argument("--metric", default=DEFAULT_METRIC)
    parser.add_argument("--max-age", type=float, default=120.0)
    args = parser.parse_args(argv)

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{args.port}/metrics", timeout=3) as resp:
            body = resp.read().decode("utf-8")
    except OSError as exc:
        print(f"unhealthy: metrics endpoint unreachable ({exc})")
        return 1

    healthy, reason = evaluate(body, args.metric, args.max_age, time.time())
    print(("healthy: " if healthy else "unhealthy: ") + reason)
    return 0 if healthy else 1


if __name__ == "__main__":
    sys.exit(main())
