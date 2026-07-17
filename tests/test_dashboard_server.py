"""Dashboard server: GET serves fresh HTML, everything else is refused,
renders are cached briefly. Uses an ephemeral port; no network beyond
localhost."""
import http.client
import json
import threading

import pandas as pd
import pytest

from tradingbot.config import AppConfig
from tradingbot.dashboard_server import DashboardServer


@pytest.fixture
def server(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    ts = pd.date_range("2026-01-01", periods=24, freq="1h", tz="UTC")
    (state / "paper_equity.csv").write_text(
        "timestamp,equity,cash,n_positions\n"
        + "\n".join(f"{t.isoformat()},{10_000 + i:.2f},10000.00,0" for i, t in enumerate(ts))
        + "\n")
    (state / "paper_state.json").write_text(json.dumps(
        {"cash": 10_000.0, "positions": [], "risk": {}, "updated": ts[-1].isoformat()}))
    cfg = AppConfig(**{"data": {"symbols": ["BTC/USDT"]},
                       "live": {"state_dir": str(state)}})
    srv = DashboardServer(("127.0.0.1", 0), cfg, min_render_interval=60.0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _request(srv, method="GET", path="/"):
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    conn.request(method, path)
    resp = conn.getresponse()
    body = resp.read().decode()
    conn.close()
    return resp.status, body


def test_get_serves_dashboard(server):
    status, body = _request(server)
    assert status == 200
    assert "tradingbot" in body and "go/no-go checklist" in body


def test_render_is_cached_within_interval(server):
    _request(server)
    first = server._cached_at
    _request(server)
    assert server._cached_at == first          # second hit within 60s: cached


def test_unknown_path_404(server):
    status, _ = _request(server, path="/etc/passwd")
    assert status == 404


def test_write_methods_refused(server):
    for method in ("POST", "PUT", "DELETE"):
        status, _ = _request(server, method=method)
        assert status == 405, f"{method} must be refused on a read-only dashboard"


def test_binds_localhost_only(server):
    assert server.server_address[0] == "127.0.0.1"
