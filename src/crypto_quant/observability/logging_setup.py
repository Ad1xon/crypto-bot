"""Logging configuration for interactive (file + Rich UI) and container (stdout JSON) modes."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

_TEXT_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


class JsonFormatter(logging.Formatter):
    """One JSON object per line — parseable by Loki, CloudWatch, `jq`."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(
    level: str = "INFO",
    log_file: str = "logs/app.log",
    log_format: str = "text",
    to_stdout: bool = False,
    retention_days: int = 5,
) -> None:
    """Install a daily-rotating file handler and, optionally, a stdout handler.

    The stdout handler is enabled in headless/container mode only: with the Rich
    dashboard active, stdout belongs to the full-screen UI.
    """
    formatter: logging.Formatter = (
        JsonFormatter() if log_format.lower() == "json" else logging.Formatter(_TEXT_FORMAT, _DATE_FORMAT)
    )

    Path(log_file).parent.mkdir(parents=True, exist_ok=True)
    file_handler = TimedRotatingFileHandler(
        filename=log_file,
        when="midnight",
        interval=1,
        backupCount=retention_days,
        encoding="utf-8",
    )
    handlers: list[logging.Handler] = [file_handler]
    if to_stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    for handler in handlers:
        handler.setFormatter(formatter)

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=handlers,
        force=True,
    )
