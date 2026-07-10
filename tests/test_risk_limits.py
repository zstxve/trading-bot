import pandas as pd
import pytest

from tradingbot.config import RiskConfig
from tradingbot.portfolio import Portfolio
from tradingbot.risk.limits import KillSwitch, RiskManager

TS = pd.Timestamp("2024-01-01 00:00", tz="UTC")
H = pd.Timedelta("1h")


def _rm(**overrides):
    cfg = RiskConfig(**{"risk_per_trade": 0.005, "max_drawdown": 0.15,
                        "daily_loss_limit": 0.02, "max_open_positions": 2,
                        "max_asset_exposure": 0.25, "max_total_exposure": 0.5,
                        **overrides})
    return RiskManager(cfg, initial_equity=10_000.0, kill_switch=KillSwitch(None))


def test_daily_loss_halts_and_resets_next_day():
    rm = _rm()
    assert rm.on_bar_close(TS, 10_000) == []
    assert rm.on_bar_close(TS + H, 9_790) == ["daily_loss_limit"]  # -2.1%
    assert rm.trading_halted
    # next UTC day: fresh daily budget
    assert rm.on_bar_close(TS + pd.Timedelta("25h"), 9_790) == []
    assert not rm.trading_halted


def test_max_drawdown_is_permanent_and_engages_kill_switch():
    rm = _rm()
    rm.on_bar_close(TS, 10_000)
    events = rm.on_bar_close(TS + H, 8_400)  # -16% from peak
    assert "max_drawdown" in events
    assert rm.hard_halted and rm.kill_switch.engaged()
    # does not reset the next day
    rm.on_bar_close(TS + pd.Timedelta("30h"), 9_000)
    assert rm.trading_halted


def test_drawdown_measured_from_peak_not_start():
    rm = _rm()
    rm.on_bar_close(TS, 12_000)              # new peak
    events = rm.on_bar_close(TS + H, 10_100) # -15.8% from 12k peak
    assert "max_drawdown" in events


def test_max_open_positions():
    rm = _rm(max_open_positions=1)
    pf = Portfolio(cash=10_000)
    pf.buy("A", qty=10, price=100, fee=0, ts=TS)
    ok, reason = rm.can_open("B", 500, pf, {"A": 100.0, "B": 100.0})
    assert not ok and reason == "max_open_positions"


def test_asset_exposure_cap():
    rm = _rm()
    pf = Portfolio(cash=10_000)
    pf.buy("A", qty=20, price=100, fee=0, ts=TS)  # 2000 notional, equity 10k
    ok, reason = rm.can_open("A", 600, pf, {"A": 100.0})
    assert not ok and reason == "max_asset_exposure"     # 26% > 25%
    ok, _ = rm.can_open("B", 600, pf, {"A": 100.0, "B": 100.0})
    assert ok


def test_total_exposure_cap():
    rm = _rm(max_asset_exposure=0.5, max_total_exposure=0.5, max_open_positions=4)
    pf = Portfolio(cash=10_000)
    pf.buy("A", qty=40, price=100, fee=0, ts=TS)  # 4000 notional
    ok, reason = rm.can_open("B", 1500, pf, {"A": 100.0, "B": 100.0})
    assert not ok and reason == "max_total_exposure"     # 55% > 50%


def test_api_error_kill_switch():
    rm = _rm(max_consecutive_api_errors=3)
    rm.on_api_error(); rm.on_api_error()
    assert not rm.kill_switch.engaged()
    rm.on_api_success()          # resets the counter
    rm.on_api_error(); rm.on_api_error(); rm.on_api_error()
    assert rm.kill_switch.engaged()
    assert rm.trading_halted


def test_stale_data_engages_kill_switch():
    rm = _rm()
    fresh = rm.check_data_fresh(TS, TS + pd.Timedelta("1.5h"), H)
    assert fresh and not rm.kill_switch.engaged()
    stale = rm.check_data_fresh(TS, TS + pd.Timedelta("3h"), H)
    assert not stale and rm.kill_switch.engaged()


def test_kill_switch_file_persists(tmp_path):
    path = tmp_path / "KILL_SWITCH"
    ks = KillSwitch(path)
    assert not ks.engaged()
    ks.engage("test reason")
    assert path.exists()
    ks2 = KillSwitch(path)               # fresh process
    assert ks2.engaged()
    assert "test reason" in ks2.reason()


def test_leverage_requires_explicit_opt_in():
    with pytest.raises(Exception):
        RiskConfig(leverage=2.0)                       # blocked
    cfg = RiskConfig(leverage=2.0, allow_leverage=True)  # explicit opt-in
    assert cfg.leverage == 2.0
