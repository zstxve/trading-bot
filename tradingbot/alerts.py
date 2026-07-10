"""Push alerts (Telegram).

Design constraints:
  - a failed or unconfigured alert must NEVER interrupt trading: send()
    swallows every exception and just returns False
  - credentials live in the environment only (TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID); config.yaml merely switches the feature on
  - stdlib urllib only — no extra dependency for a 20-line HTTP POST
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from typing import Optional

from tradingbot.config import AlertsConfig

_API = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    def __init__(self, cfg: AlertsConfig, logger: Optional[logging.Logger] = None):
        self.cfg = cfg
        self.log = logger
        self.token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.enabled = cfg.telegram_enabled and bool(self.token) and bool(self.chat_id)
        if cfg.telegram_enabled and not self.enabled and logger:
            logger.warning("alerts.telegram_enabled is true but TELEGRAM_BOT_TOKEN/"
                           "TELEGRAM_CHAT_ID are not set — alerts disabled")

    def send(self, text: str) -> bool:
        """Deliver a message. Returns False (never raises) on any failure."""
        if not self.enabled:
            return False
        try:
            payload = json.dumps({"chat_id": self.chat_id, "text": text}).encode("utf-8")
            req = urllib.request.Request(
                _API.format(token=self.token), data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                ok = 200 <= resp.status < 300
            if not ok and self.log:
                self.log.warning("telegram alert rejected: HTTP %s", resp.status)
            return ok
        except Exception as exc:  # noqa: BLE001 — alerting must never take down the loop
            if self.log:
                self.log.warning("telegram alert failed: %s", exc)
            return False
