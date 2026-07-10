"""Offline tests for the divergence-report helpers and the HTML dashboard,
using fabricated state files in tmp_path (no network, no runner)."""
import json

import pandas as pd
import pytest

from tradingbot.config import AppConfig
from tradingbot.dashboard import generate_dashboard
from tradingbot.report import divergence_notes, load_paper_history, side_by_side


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    ts = pd.date_range("2026-01-01", periods=48, freq="1h", tz="UTC")
    eq = "\n".join(f"{t.isoformat()},{10_000 + i * 5:.2f},{9_000:.2f},1"
                   for i, t in enumerate(ts))
    (d / "paper_equity.csv").write_text("timestamp,equity,cash,n_positions\n" + eq + "\n")
    (d / "paper_trades.csv").write_text(
        "symbol,entry_time,exit_time,entry_price,exit_price,qty,pnl,fees,"
        "return_pct,bars_held,exit_reason\n"
        f"BTC/USDT,{ts[0].isoformat()},{ts[10].isoformat()},100,105,1.0,4.8,0.2,"
        "0.048,10,mean_reverted\n"
        f"ETH/USDT,{ts[5].isoformat()},{ts[20].isoformat()},50,49,2.0,-2.2,0.2,"
        "-0.022,15,stop\n"
    )
    (d / "paper_state.json").write_text(json.dumps({
        "cash": 9_000.0,
        "positions": [{"symbol": "SOL/USDT", "qty": 5.0, "entry_price": 40.0,
                       "entry_time": ts[30].isoformat(), "stop_price": 36.0,
                       "take_profit": None, "entry_fee": 0.1, "bars_held": 3}],
        "risk": {"peak_equity": 10_240.0, "day_start_equity": 10_200.0},
        "updated": ts[-1].isoformat(),
    }))
    return d


def test_load_paper_history(state_dir):
    equity, trades = load_paper_history(state_dir)
    assert len(equity) == 48
    assert equity.iloc[-1] == pytest.approx(10_000 + 47 * 5)
    assert len(trades) == 2
    assert trades[1].exit_reason == "stop"
    assert trades[0].pnl == pytest.approx(4.8)


def test_load_paper_history_empty(tmp_path):
    equity, trades = load_paper_history(tmp_path)
    assert equity is None and trades == []


def test_side_by_side_formats_all_rows():
    m = {"total_return": 0.05, "cagr": 0.1, "sharpe": 1.2, "sortino": 1.5,
         "max_drawdown": -0.04, "n_trades": 12.0, "win_rate": 0.5,
         "profit_factor": 1.4, "avg_trade_pct": 0.004, "avg_bars_held": 20.0,
         "exposure": 0.4}
    out = side_by_side(m, m)
    assert "total_return" in out and "5.00%" in out and "12" in out


def test_divergence_notes_flag_small_sample_and_costs():
    paper = {"n_trades": 5, "avg_trade_pct": -0.004, "max_drawdown": -0.05}
    backtest = {"n_trades": 20, "avg_trade_pct": 0.002, "max_drawdown": -0.02}
    notes = divergence_notes(paper, backtest, n_paper_trades=5)
    joined = " ".join(notes)
    assert "statistically weak" in joined
    assert "trade count diverges" in joined
    assert "slippage" in joined


def test_dashboard_generates_selfcontained_html(state_dir):
    cfg = AppConfig(**{"data": {"symbols": ["BTC/USDT"]},
                       "live": {"state_dir": str(state_dir)}})
    out = generate_dashboard(cfg)
    html_text = out.read_text()
    assert out.name == "dashboard.html"
    assert "SOL/USDT" in html_text                  # open position rendered
    assert "mean_reverted" in html_text             # trade table rendered
    assert "data:image/png;base64," in html_text    # embedded chart, no ext refs
    assert "http://" not in html_text.replace("http://www.apple.com", "")


def test_dashboard_shows_kill_switch(state_dir):
    (state_dir / "KILL_SWITCH").write_text("2026-01-02 max_drawdown breached")
    cfg = AppConfig(**{"data": {"symbols": ["BTC/USDT"]},
                       "live": {"state_dir": str(state_dir)}})
    html_text = generate_dashboard(cfg).read_text()
    assert "KILL SWITCH ENGAGED" in html_text
    assert "max_drawdown breached" in html_text
