"""Read-only status dashboard: one self-contained HTML file.

Renders paper/live state (equity curve, positions, recent trades, kill-switch
status) into `state/dashboard.html`. No server, no controls, no JavaScript
beyond a meta-refresh — deliberately: the kill switch stays a file, and
nothing about trading is clickable.
"""
from __future__ import annotations

import base64
import html
import io
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pandas as pd

from tradingbot.config import AppConfig
from tradingbot.report import load_paper_history


def _equity_png_b64(equity: pd.Series) -> Optional[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if equity is None or len(equity) < 2:
        return None
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 5), sharex=True,
                                   gridspec_kw={"height_ratios": [3, 1]})
    ax1.plot(equity.index, equity.values, lw=1.2)
    ax1.set_title("paper equity")
    ax1.grid(alpha=0.3)
    dd = equity / equity.cummax() - 1
    ax2.fill_between(dd.index, dd * 100, 0, color="tab:red", alpha=0.4)
    ax2.set_title("drawdown (%)")
    ax2.grid(alpha=0.3)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _table(rows, headers) -> str:
    th = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    trs = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in row) + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"


def generate_dashboard(cfg: AppConfig, prefix: str = "paper") -> Path:
    state_dir = Path(cfg.live.state_dir)
    state_path = state_dir / f"{prefix}_state.json"
    kill_path = state_dir / "KILL_SWITCH"

    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    equity, trades = load_paper_history(state_dir, prefix)

    kill_html = ""
    if kill_path.exists():
        kill_html = (f'<div class="kill">KILL SWITCH ENGAGED: '
                     f'{html.escape(kill_path.read_text().strip())}</div>')

    cash = state.get("cash")
    positions = state.get("positions", [])
    last_equity = float(equity.iloc[-1]) if equity is not None and len(equity) else cash
    stat_cells = [
        ("mode", prefix.upper()),
        ("strategy", cfg.strategy.active),
        ("equity", f"{last_equity:,.2f}" if last_equity is not None else "n/a"),
        ("cash", f"{cash:,.2f}" if cash is not None else "n/a"),
        ("open positions", len(positions)),
        ("closed trades", len(trades)),
        ("state updated", state.get("updated", "n/a")),
    ]
    stats = "".join(f'<div class="stat"><div class="k">{html.escape(str(k))}</div>'
                    f'<div class="v">{html.escape(str(v))}</div></div>' for k, v in stat_cells)

    pos_rows = [(p["symbol"], f'{p["qty"]:.8f}', f'{p["entry_price"]:.4f}',
                 f'{p.get("stop_price") or "-"}', p["entry_time"], p.get("bars_held", 0))
                for p in positions]
    pos_html = _table(pos_rows, ["symbol", "qty", "entry", "stop", "entry time", "bars held"]) \
        if pos_rows else "<p>flat — no open positions</p>"

    recent = trades[-20:][::-1]
    tr_rows = [(t.symbol, f"{t.pnl:+.2f}", f"{t.return_pct:+.2%}", t.exit_reason,
                str(t.exit_time)) for t in recent]
    tr_html = _table(tr_rows, ["symbol", "pnl", "return", "exit reason", "closed at"]) \
        if tr_rows else "<p>no closed trades yet</p>"

    png = _equity_png_b64(equity)
    chart = (f'<img src="data:image/png;base64,{png}" alt="equity curve">'
             if png else "<p>not enough equity history for a chart yet</p>")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>tradingbot — {prefix}</title>
<style>
 body {{ font-family: -apple-system, system-ui, sans-serif; margin: 2rem; color: #1a1a1a; }}
 h1 {{ font-size: 1.3rem; }} h2 {{ font-size: 1.05rem; margin-top: 1.6rem; }}
 .kill {{ background: #c0392b; color: #fff; padding: .8rem 1rem; border-radius: 6px;
          font-weight: 700; margin-bottom: 1rem; }}
 .stats {{ display: flex; flex-wrap: wrap; gap: .8rem; }}
 .stat {{ border: 1px solid #ddd; border-radius: 6px; padding: .55rem .9rem; }}
 .stat .k {{ font-size: .72rem; color: #777; text-transform: uppercase; }}
 .stat .v {{ font-size: 1.05rem; font-weight: 600; }}
 table {{ border-collapse: collapse; margin-top: .4rem; }}
 th, td {{ border: 1px solid #ddd; padding: .35rem .7rem; font-size: .85rem; text-align: right; }}
 th:first-child, td:first-child {{ text-align: left; }}
 img {{ max-width: 100%; border: 1px solid #eee; border-radius: 6px; }}
 footer {{ margin-top: 2rem; color: #999; font-size: .78rem; }}
</style></head><body>
<h1>tradingbot — {prefix} session</h1>
{kill_html}
<div class="stats">{stats}</div>
<h2>equity</h2>{chart}
<h2>open positions</h2>{pos_html}
<h2>recent trades (last 20)</h2>{tr_html}
<footer>generated {now} · read-only · auto-refreshes every 60s ·
regenerate with <code>python -m tradingbot dashboard</code></footer>
</body></html>"""

    out = state_dir / "dashboard.html"
    state_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    return out
