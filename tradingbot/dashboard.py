"""Read-only status dashboard: one self-contained HTML file.

Answers two questions at a glance:
  1. Where do we stand?   — session health, validation clock, equity, trades,
     latest validated walk-forward reference, paper-vs-backtest divergence.
  2. What still needs improving? — a live go/no-go checklist computed from
     real data plus an auto-generated, priority-sorted action list.

Design constraints (deliberate):
  - no server, no JavaScript beyond meta-refresh, no controls — the kill
    switch stays a file and nothing about trading is clickable
  - offline: reads state files, cached candles and stored reports only;
    never fetches from the network
  - honest: every checklist verdict shows the number it was computed from,
    and missing data reads as "pending", never as "passed"
"""
from __future__ import annotations

import base64
import html
import io
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from tradingbot.config import AppConfig
from tradingbot.report import divergence_notes, load_paper_history, side_by_side

# Validation targets (mirrors the docs/GUIDE.md go/no-go checklist).
TARGET_PAPER_DAYS = 90
TARGET_TRADES = 100
MIN_OOS_SHARPE = 1.0
MIN_OOS_PF = 1.3
MAX_OOS_DD = -0.10          # want margin inside the -15% hard limit
HARD_DD_LIMIT = -0.15


# --------------------------------------------------------------------- utils
def _sh(cmd: List[str]) -> str:
    """Best-effort local command; empty string on any failure."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return out.stdout or ""
    except Exception:  # noqa: BLE001 — dashboard must render regardless
        return ""


def _esc(x) -> str:
    return html.escape(str(x))


def _badge(status: str) -> str:
    cls = {"ok": "b-ok", "warn": "b-warn", "fail": "b-fail", "pend": "b-pend"}[status]
    txt = {"ok": "PASS", "warn": "CHECK", "fail": "FAIL", "pend": "PENDING"}[status]
    return f'<span class="badge {cls}">{txt}</span>'


def _table(rows, headers) -> str:
    th = "".join(f"<th>{_esc(h)}</th>" for h in headers)
    trs = "".join(
        "<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows
    )
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def _progress(frac: float, label: str) -> str:
    pct = max(0.0, min(1.0, frac)) * 100
    color = "#1e8e3e" if frac >= 1.0 else "#4285f4"
    return (f'<div class="progress-label">{_esc(label)}</div>'
            f'<div class="progress"><div class="bar" '
            f'style="width:{pct:.1f}%;background:{color}"></div></div>')


# ---------------------------------------------------------------- gathering
def _supervisor_status(prefix: str) -> Tuple[str, str, str]:
    """(status, severity, detail) for how the loop is being kept alive."""
    listing = _sh(["launchctl", "list"])
    agent_line = next((ln for ln in listing.splitlines() if "com.tradingbot.paperloop" in ln), "")
    procs = _sh(["pgrep", "-fl", f"tradingbot {prefix}"])
    proc_alive = bool(procs.strip())

    if agent_line:
        parts = agent_line.split()
        pid, code = parts[0], parts[1]
        if pid != "-":
            return "supervised (launchd)", "ok", f"agent running, pid {pid}"
        if proc_alive:
            return "interim process (launchd broken)", "warn", \
                f"launchd exit code {code}; loop alive via manual process — reboot to fix the agent"
        return "NOT RUNNING", "fail", f"launchd exit code {code} and no manual process found"
    if proc_alive:
        return "unsupervised process", "warn", "running, but nothing restarts it after a crash/reboot"
    return "NOT RUNNING", "fail", "no launchd agent and no process — start the paper loop"


def _latest_wf_reference(cfg: AppConfig) -> Tuple[Optional[dict], str, Optional[str]]:
    """Newest stored walk-forward OOS metrics MATCHING the active strategy and
    timeframe (via run_info.json). Reports for other configurations — e.g. a
    1h experiment while the dashboard tracks a 1d session — are skipped, and
    reports without metadata are never trusted."""
    candidates = sorted(Path("reports").glob("walkforward_*/OOS_metrics.json"), reverse=True)
    for path in candidates:
        info_path = path.parent / "run_info.json"
        if not info_path.exists():
            continue
        try:
            info = json.loads(info_path.read_text())
            if info.get("strategy") != cfg.strategy.active:
                continue
            if info.get("timeframe") != cfg.data.timeframe:
                continue
            metrics = json.loads(path.read_text())
        except Exception:  # noqa: BLE001
            continue
        stability = None
        windows = path.parent / "windows.txt"
        if windows.exists():
            m = re.search(r"Distinct parameter sets chosen: (\d+)/(\d+)", windows.read_text())
            if m:
                stability = f"{m.group(1)}/{m.group(2)}"
        return metrics, path.parent.name, stability
    return None, "", None


def _data_gaps(equity: Optional[pd.Series], bar: pd.Timedelta) -> Tuple[int, int]:
    """(missing_bars, expected_bars) in the recorded equity history."""
    if equity is None or len(equity) < 2:
        return 0, 0
    expected = int((equity.index[-1] - equity.index[0]) / bar) + 1
    return max(0, expected - len(equity)), expected


def _log_warnings(log_dir: str, prefix: str, n: int = 5) -> List[str]:
    logs = sorted(Path(log_dir).glob(f"{prefix}_*.log"))
    if not logs:
        return []
    lines = logs[-1].read_text(encoding="utf-8", errors="replace").splitlines()
    return [ln for ln in lines if " WARNING " in ln or " ERROR " in ln][-n:]


def _offline_divergence(cfg: AppConfig, equity: pd.Series, trades) -> Optional[str]:
    """Paper vs backtest over the identical window, cache-only. None if the
    cached candles do not cover the paper period."""
    from tradingbot.backtest.engine import run_backtest
    from tradingbot.backtest.metrics import compute_metrics
    from tradingbot.costs import CostModel
    from tradingbot.data.loader import load_cached_history
    from tradingbot.strategies.registry import build_strategy

    if equity is None or len(equity) < 3:
        return None
    cached = load_cached_history(cfg.data)
    if not cached:
        return None
    start, end = equity.index[0], equity.index[-1]
    if any(df.index[-1] < end or df.index[0] > start for df in cached.values()):
        return None

    strategy = build_strategy(cfg.strategy.active, cfg.strategy.active_params())
    prepared = {}
    for s, df in cached.items():
        p = strategy.prepare(df)
        p = p[(p.index >= start - pd.Timedelta("1h")) & (p.index <= end)]
        if len(p) > 2:
            prepared[s] = p
    if not prepared:
        return None

    initial = float(equity.iloc[0])
    ppy = cfg.backtest.periods_per_year
    res = run_backtest(prepared, strategy, cfg.risk,
                       CostModel(**cfg.costs.model_dump()), initial)
    bt_m = compute_metrics(res.equity, res.trades, ppy, initial,
                           res.turnover_notional, res.exposure)
    paper_m = compute_metrics(equity, trades, ppy, initial)
    notes = divergence_notes(paper_m, bt_m, len(trades))
    return (f"<pre>{_esc(side_by_side(paper_m, bt_m))}</pre>"
            "<ul>" + "".join(f"<li>{_esc(n)}</li>" for n in notes) + "</ul>")


# ------------------------------------------------------------------- charts
def _chart_b64(equity: Optional[pd.Series], trades) -> Optional[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if equity is None or len(equity) < 2:
        return None
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10.5, 5.5), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})
    ax1.plot(equity.index, equity.values, lw=1.3, color="#4285f4")
    for t in trades:
        marker_ts = min(max(t.exit_time, equity.index[0]), equity.index[-1])
        y = float(equity.reindex([marker_ts], method="nearest").iloc[0])
        ax1.plot(marker_ts, y, marker="v" if t.pnl < 0 else "^",
                 color="#c0392b" if t.pnl < 0 else "#1e8e3e", ms=7)
    ax1.set_title("paper equity (markers = closed trades)")
    ax1.grid(alpha=0.3)
    dd = equity / equity.cummax() - 1
    ax2.fill_between(dd.index, dd * 100, 0, color="#c0392b", alpha=0.4)
    ax2.axhline(HARD_DD_LIMIT * 100, color="#c0392b", lw=1, ls="--")
    ax2.set_title("drawdown (%) — dashed line = kill level")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ---------------------------------------------------------------- assembly
def generate_dashboard(
    cfg: AppConfig,
    prefix: str = "paper",
    live_marks: Optional[Dict[str, float]] = None,
    marks_ts: Optional[pd.Timestamp] = None,
) -> Path:
    """`live_marks` ({symbol: last price}) comes from the runner's intra-bar
    ticker poll; with it the page shows a live equity estimate and unrealized
    PnL instead of waiting for the next bar close. When not passed explicitly,
    fresh marks are picked up from state/live_marks.json (written by the
    runner), so out-of-process renders — dashboard server, CLI — stay live."""
    from tradingbot.data.loader import TIMEFRAME_SECONDS, tf_timedelta

    state_dir = Path(cfg.live.state_dir)
    if live_marks is None:
        marks_path = state_dir / "live_marks.json"
        if marks_path.exists():
            try:
                raw = json.loads(marks_path.read_text())
                ts = pd.Timestamp(raw["ts"]) if raw.get("ts") else None
                fresh = ts is not None and \
                    (pd.Timestamp.now(tz="UTC") - ts) < pd.Timedelta(seconds=3 * cfg.live.poll_seconds)
                if fresh and raw.get("marks"):
                    live_marks = {k: float(v) for k, v in raw["marks"].items()}
                    marks_ts = ts
            except Exception:  # noqa: BLE001 — stale/corrupt marks: render without
                pass
    state_path = state_dir / f"{prefix}_state.json"
    kill_path = state_dir / "KILL_SWITCH"
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    equity, trades = load_paper_history(state_dir, prefix)
    bar = tf_timedelta(cfg.data.timeframe)
    now = datetime.now(timezone.utc)

    # ---- session health ----------------------------------------------------
    sup_status, sup_sev, sup_detail = _supervisor_status(prefix)
    stale_note, stale_sev = "", "ok"
    if equity is not None and len(equity):
        age = now - equity.index[-1].to_pydatetime()
        if age > cfg.risk.stale_data_factor * bar:
            stale_sev = "warn"
            stale_note = f"last equity record is {age} old"
    telegram_on = cfg.alerts.telegram_enabled
    git_remote = bool(_sh(["git", "remote"]).strip())
    gaps, expected_bars = _data_gaps(equity, bar)

    # ---- validation clock ---------------------------------------------------
    wf_metrics, wf_run, wf_stability = _latest_wf_reference(cfg)
    paper_days = (equity.index[-1] - equity.index[0]).days if equity is not None and len(equity) > 1 else 0
    wf_trades = int(wf_metrics.get("n_trades", 0)) if wf_metrics else 0
    total_trades = len(trades) + wf_trades

    # ---- go/no-go checklist --------------------------------------------------
    checks: List[Tuple[str, str, str]] = []

    def add_check(name: str, status: str, detail: str):
        checks.append((name, status, detail))

    if wf_metrics:
        sh = wf_metrics.get("sharpe", 0.0)
        add_check(f"walk-forward OOS Sharpe > {MIN_OOS_SHARPE}",
                  "ok" if sh > MIN_OOS_SHARPE else "fail",
                  f"{sh:.2f} ({wf_run})")
        pf = wf_metrics.get("profit_factor", 0.0)
        add_check(f"walk-forward OOS profit factor > {MIN_OOS_PF}",
                  "ok" if pf > MIN_OOS_PF else "fail", f"{pf:.2f}")
        dd = wf_metrics.get("max_drawdown", 0.0)
        dd_status = "ok" if dd > MAX_OOS_DD else ("warn" if dd > HARD_DD_LIMIT else "fail")
        add_check("walk-forward OOS max drawdown < 10% (margin inside 15% kill)",
                  dd_status, f"{dd:.1%}")
    else:
        add_check("walk-forward OOS metrics available", "pend",
                  f"no stored report matches {cfg.strategy.active}/{cfg.data.timeframe} "
                  "— run walkforward with this config")

    add_check(f"parameter stability across windows",
              "ok" if wf_stability and int(wf_stability.split("/")[0]) <= int(wf_stability.split("/")[1]) // 3
              else ("warn" if wf_stability else "pend"),
              f"{wf_stability or 'no windows.txt found'} distinct sets (fewer = better)")

    add_check(f"paper phase ≥ {TARGET_PAPER_DAYS} days",
              "ok" if paper_days >= TARGET_PAPER_DAYS else "pend",
              f"{paper_days} days so far")
    add_check(f"OOS + paper trades ≥ {TARGET_TRADES}",
              "ok" if total_trades >= TARGET_TRADES else "pend",
              f"{wf_trades} OOS + {len(trades)} paper = {total_trades}")

    divergence_html = _offline_divergence(cfg, equity, trades)
    if len(trades) >= 10 and divergence_html:
        clean = "no gross divergence" in divergence_html
        add_check("paper matches backtest over identical window",
                  "ok" if clean else "warn", "see divergence section below")
    else:
        add_check("paper matches backtest over identical window", "pend",
                  f"{len(trades)} paper trades — comparison meaningful from ~10")

    add_check("you can explain WHY the edge exists (one sentence)", "pend",
              "human judgement — the dashboard cannot check this for you")

    # ---- improvements / action list ------------------------------------------
    actions: List[Tuple[str, str]] = []   # (severity, text)
    if kill_path.exists():
        actions.append(("fail", "KILL SWITCH engaged — investigate, then delete state/KILL_SWITCH"))
    if sup_sev != "ok":
        actions.append((sup_sev, f"supervision: {sup_detail}"))
    if stale_sev != "ok":
        actions.append(("warn", f"stale session: {stale_note} — is the loop alive?"))
    if gaps > 0:
        actions.append(("warn", f"{gaps} missing bars of {expected_bars} in the equity record "
                                "(machine asleep?) — move the loop to an always-on box/VPS"))
    if not telegram_on:
        actions.append(("warn", "Telegram alerts disabled — set TELEGRAM_BOT_TOKEN/CHAT_ID in .env "
                                "and alerts.telegram_enabled: true"))
    if not git_remote:
        actions.append(("warn", "git has no remote — push to a private repo for backup"))
    for name, status, detail in checks:
        if status == "fail":
            actions.append(("fail", f"checklist FAIL: {name} ({detail})"))
    if not actions:
        actions.append(("ok", "nothing actionable — let the validation clock run and check weekly"))

    # ---- html assembly ---------------------------------------------------------
    kill_html = (f'<div class="kill">KILL SWITCH ENGAGED: '
                 f'{_esc(kill_path.read_text().strip())}</div>') if kill_path.exists() else ""

    cash = state.get("cash")
    positions = state.get("positions", [])
    last_equity = float(equity.iloc[-1]) if equity is not None and len(equity) else cash

    # Live equity estimate: cash + positions marked at the freshest tickers.
    live_equity = None
    if live_marks and cash is not None and positions \
            and all(p["symbol"] in live_marks for p in positions):
        live_equity = cash + sum(p["qty"] * live_marks[p["symbol"]] for p in positions)
    shown_equity = live_equity if live_equity is not None else last_equity
    equity_label = "equity (live est.)" if live_equity is not None else "equity (bar close)"
    total_ret = (shown_equity / float(equity.iloc[0]) - 1) \
        if equity is not None and len(equity) > 1 and shown_equity is not None else 0.0

    # Countdown to the next bar close (when the next decision happens).
    tf_s = TIMEFRAME_SECONDS[cfg.data.timeframe]
    secs_left = int((int(now.timestamp()) // tf_s + 1) * tf_s - now.timestamp())
    next_bar = f"{secs_left // 3600}h {secs_left % 3600 // 60:02d}m"

    sev_color = {"ok": "#1e8e3e", "warn": "#f9ab00", "fail": "#c0392b"}
    stat_cells = [
        ("session", prefix.upper()),
        ("strategy", cfg.strategy.active),
        ("timeframe", cfg.data.timeframe),
        (equity_label, f"{shown_equity:,.2f}" if shown_equity is not None else "n/a"),
        ("return", f"{total_ret:+.2%}"),
        ("cash", f"{cash:,.2f}" if cash is not None else "n/a"),
        ("open positions", len(positions)),
        ("closed trades", len(trades)),
        ("next decision in", next_bar),
        ("supervision", f'<span style="color:{sev_color[sup_sev]}">{_esc(sup_status)}</span>'),
        ("alerts", "telegram ON" if telegram_on else
         f'<span style="color:{sev_color["warn"]}">disabled</span>'),
    ]
    stats = "".join(f'<div class="stat"><div class="k">{_esc(k)}</div>'
                    f'<div class="v">{v}</div></div>' for k, v in stat_cells)

    clock = (_progress(paper_days / TARGET_PAPER_DAYS,
                       f"paper days: {paper_days} / {TARGET_PAPER_DAYS}")
             + _progress(total_trades / TARGET_TRADES,
                         f"trades (OOS + paper): {total_trades} / {TARGET_TRADES}"))
    if equity is not None and len(equity) > 1 and paper_days < TARGET_PAPER_DAYS:
        eta = equity.index[0] + pd.Timedelta(days=TARGET_PAPER_DAYS)
        clock += f'<p class="muted">go/no-go review earliest: {eta.date()}</p>'

    checklist_html = _table(
        [(f"{_badge(s)}", _esc(n), _esc(d)) for n, s, d in checks],
        ["verdict", "criterion", "measured"],
    )
    actions_html = "".join(
        f'<li><span class="badge b-{ "fail" if s == "fail" else ("warn" if s == "warn" else "ok")}">'
        f'{s.upper()}</span> {_esc(t)}</li>' for s, t in actions)

    png = _chart_b64(equity, trades)
    chart = (f'<img src="data:image/png;base64,{png}" alt="equity curve">'
             if png else "<p>not enough equity history for a chart yet</p>")

    pos_headers = ["symbol", "qty", "entry", "stop", "entry time", "bars held"]
    pos_rows = []
    for p in positions:
        row = [_esc(p["symbol"]), f'{p["qty"]:.8f}', f'{p["entry_price"]:.4f}',
               _esc(p.get("stop_price") or "-"), _esc(p["entry_time"]),
               p.get("bars_held", 0)]
        if live_marks and p["symbol"] in live_marks:
            mark = live_marks[p["symbol"]]
            upnl = (mark - p["entry_price"]) * p["qty"]
            color = "#1e8e3e" if upnl >= 0 else "#c0392b"
            row += [f"{mark:.4f}", f'<span style="color:{color}">{upnl:+,.2f}</span>']
        pos_rows.append(row)
    if live_marks and positions:
        pos_headers += ["live mark", "unrealized"]
    pos_html = _table(pos_rows, pos_headers) if pos_rows else "<p>flat — no open positions</p>"

    tr_rows = [(_esc(t.symbol), f"{t.pnl:+.2f}", f"{t.return_pct:+.2%}",
                _esc(t.exit_reason), _esc(str(t.exit_time))) for t in trades[-20:][::-1]]
    tr_html = _table(tr_rows, ["symbol", "pnl", "return", "exit reason", "closed at"]) \
        if tr_rows else ("<p>no closed trades yet — breakout entries need a fresh "
                         f"{cfg.strategy.breakout_donchian.entry_n}-bar high, patience is the strategy</p>"
                         if cfg.strategy.active == "breakout_donchian" else "<p>no closed trades yet</p>")

    if wf_metrics:
        ref_rows = [(_esc(k), _esc(f"{v:.2%}" if k in ("total_return", "cagr", "max_drawdown", "win_rate",
                                                       "avg_trade_pct", "exposure") else
                          (f"{int(v)}" if k == "n_trades" else f"{v:.2f}")))
                    for k, v in wf_metrics.items()
                    if k in ("total_return", "cagr", "sharpe", "sortino", "max_drawdown",
                             "n_trades", "win_rate", "profit_factor", "exposure")]
        ref_html = _table(ref_rows, ["metric", "walk-forward OOS"]) + \
            f'<p class="muted">source: reports/{_esc(wf_run)} — param stability {_esc(wf_stability or "n/a")}</p>'
    else:
        ref_html = "<p>no stored walk-forward report — run <code>python -m tradingbot walkforward</code></p>"

    warn_lines = _log_warnings(cfg.logging.dir, prefix)
    ops_html = (
        f"<p>supervision: <b>{_esc(sup_status)}</b> — {_esc(sup_detail)}<br>"
        f"data gaps: {gaps} of {expected_bars} bars missing"
        f"{' — ' + _esc(stale_note) if stale_note else ''}<br>"
        f"git remote: {'configured' if git_remote else 'none (no backup)'}</p>"
        + ("<pre>" + _esc("\n".join(warn_lines)) + "</pre>" if warn_lines
           else "<p class='muted'>no recent warnings/errors in the newest session log</p>")
    )

    divergence_section = divergence_html or \
        "<p class='muted'>needs a few days of paper history plus cached candles covering the window</p>"

    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>tradingbot — {prefix}</title>
<style>
 :root {{ --bg: #ffffff; --fg: #1a1a1a; --muted: #8a8a8a; --border: #ddd;
          --soft: #f6f8fa; --track: #ececec; }}
 @media (prefers-color-scheme: dark) {{
   :root {{ --bg: #131417; --fg: #e8eaed; --muted: #9aa0a6; --border: #3c4043;
            --soft: #1e2126; --track: #2a2d33; }}
 }}
 body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2rem auto;
        max-width: 1080px; padding: 0 1rem; color: var(--fg); background: var(--bg); }}
 h1 {{ font-size: 1.35rem; }} h2 {{ font-size: 1.05rem; margin-top: 1.8rem;
      border-bottom: 1px solid var(--border); padding-bottom: .3rem; }}
 .kill {{ background: #c0392b; color: #fff; padding: .8rem 1rem; border-radius: 6px;
          font-weight: 700; margin-bottom: 1rem; }}
 .stats {{ display: flex; flex-wrap: wrap; gap: .7rem; }}
 .stat {{ border: 1px solid var(--border); border-radius: 6px; padding: .5rem .85rem; }}
 .stat .k {{ font-size: .7rem; color: var(--muted); text-transform: uppercase; }}
 .stat .v {{ font-size: 1.02rem; font-weight: 600; }}
 table {{ border-collapse: collapse; margin-top: .4rem; }}
 th, td {{ border: 1px solid var(--border); padding: .35rem .7rem; font-size: .85rem;
          text-align: left; }}
 .badge {{ padding: .1rem .5rem; border-radius: 4px; color: #fff; font-size: .72rem;
          font-weight: 700; }}
 .b-ok {{ background: #1e8e3e; }} .b-warn {{ background: #b8860b; }}
 .b-fail {{ background: #c0392b; }} .b-pend {{ background: #9aa0a6; }}
 .progress {{ background: var(--track); border-radius: 5px; height: 14px;
             margin: .25rem 0 .8rem; }}
 .progress .bar {{ height: 100%; border-radius: 5px; }}
 .progress-label {{ font-size: .85rem; }}
 img {{ max-width: 100%; border: 1px solid var(--border); border-radius: 6px;
       background: #fff; }}
 pre {{ background: var(--soft); padding: .7rem; border-radius: 6px; overflow-x: auto;
       font-size: .8rem; }}
 ul {{ padding-left: 1.2rem; }} li {{ margin: .3rem 0; }}
 .muted {{ color: var(--muted); font-size: .82rem; }}
 footer {{ margin-top: 2.2rem; color: var(--muted); font-size: .78rem; }}
</style></head><body>
<h1>tradingbot — {prefix} session</h1>
{kill_html}
<div class="stats">{stats}</div>

<h2>validation clock</h2>
{clock}

<h2>go/no-go checklist (live)</h2>
{checklist_html}

<h2>what to improve next</h2>
<ul>{actions_html}</ul>

<h2>equity</h2>
{chart}

<h2>paper vs backtest (identical window, offline)</h2>
{divergence_section}

<h2>open positions</h2>
{pos_html}

<h2>recent trades (last 20)</h2>
{tr_html}

<h2>validated reference (latest walk-forward)</h2>
{ref_html}

<h2>ops health</h2>
{ops_html}

<footer>generated {now.strftime("%Y-%m-%d %H:%M:%S UTC")}{
    " · live marks " + marks_ts.strftime("%H:%M:%S UTC") if marks_ts is not None else ""}
· auto-regenerated every {cfg.live.dashboard_refresh_seconds}s by the runner · read-only ·
regenerate manually: <code>python -m tradingbot dashboard --config config_daily.yaml</code></footer>
</body></html>"""

    out = state_dir / "dashboard.html"
    state_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    return out
