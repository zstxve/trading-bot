"""Live dashboard server.

A tiny stdlib HTTP server that regenerates the dashboard on demand, so the
page is current on every load instead of waiting for the runner's periodic
rewrite. The browser's meta-refresh (60s) makes an open tab self-updating.

Security posture (deliberate):
  - binds 127.0.0.1 only — never exposed to the network
  - GET-only; any other method gets 405
  - read-only: rendering never mutates trading state; the kill switch stays
    a file and there are no controls to click
  - renders are rate-limited (min interval) so a hot refresh loop cannot
    turn the dashboard into a CPU problem
"""
from __future__ import annotations

import logging
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from tradingbot.config import AppConfig

_PATHS = ("/", "/dashboard", "/dashboard.html")


class _Handler(BaseHTTPRequestHandler):
    server_version = "tradingbot-dashboard"

    def do_GET(self):  # noqa: N802 — http.server API
        if self.path.split("?")[0] not in _PATHS:
            self.send_error(404, "only / is served")
            return
        try:
            body = self.server.render().encode("utf-8")  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — show the error, don't die
            body = (f"<h1>dashboard render failed</h1><pre>{exc}</pre>"
                    "<p>see logs; the trading loop is unaffected</p>").encode()
            self.send_response(500)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        self.send_error(405, "read-only dashboard")

    do_PUT = do_DELETE = do_PATCH = do_POST

    def log_message(self, fmt, *args):  # quiet: route to debug logging
        logging.getLogger("tradingbot.dashboard").debug(fmt, *args)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, cfg: AppConfig, prefix: str = "paper",
                 min_render_interval: float = 5.0):
        super().__init__(addr, _Handler)
        self._cfg = cfg
        self._prefix = prefix
        self._min_interval = min_render_interval
        self._lock = threading.Lock()
        self._cached: Optional[str] = None
        self._cached_at = 0.0

    def render(self) -> str:
        from tradingbot.dashboard import generate_dashboard

        with self._lock:
            now = time.monotonic()
            if self._cached is not None and now - self._cached_at < self._min_interval:
                return self._cached
            path = generate_dashboard(self._cfg, prefix=self._prefix)
            self._cached = path.read_text(encoding="utf-8")
            self._cached_at = now
            return self._cached


def serve(cfg: AppConfig, port: int = 8791, prefix: str = "paper",
          logger: Optional[logging.Logger] = None) -> None:
    server = DashboardServer(("127.0.0.1", port), cfg, prefix)
    url = f"http://127.0.0.1:{port}/"
    msg = f"live dashboard at {url} (read-only, localhost only) — Ctrl-C to stop"
    print(msg)
    if logger:
        logger.info(msg)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
