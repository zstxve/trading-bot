"""Structured logging.

Every signal, order, fill, rejection, risk breach and portfolio update goes
through `log_event`, which attaches machine-readable key/value fields to the
record. The file handler can emit JSON lines (logging.json_logs: true) for
later analysis; the console always stays human-readable.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from tradingbot.config import LoggingConfig

_STD_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class HumanFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        evt = getattr(record, "evt", None)
        if evt:
            base += " | " + " ".join(f"{k}={v}" for k, v in evt.items())
        return base


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update(getattr(record, "evt", {}) or {})
        return json.dumps(payload, default=str)


def setup_logging(cfg: LoggingConfig, run_name: str) -> logging.Logger:
    """Configure the 'tradingbot' logger with console + per-run file output."""
    logger = logging.getLogger("tradingbot")
    logger.setLevel(cfg.level)
    logger.handlers.clear()
    logger.propagate = False

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(HumanFormatter(_STD_FMT))
    logger.addHandler(console)

    log_dir = Path(cfg.dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    fileh = logging.FileHandler(log_dir / f"{run_name}.log", encoding="utf-8")
    fileh.setFormatter(JsonFormatter() if cfg.json_logs else HumanFormatter(_STD_FMT))
    logger.addHandler(fileh)
    return logger


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields) -> None:
    """Log a structured event: message plus key=value fields."""
    logger.log(level, event, extra={"evt": fields})
