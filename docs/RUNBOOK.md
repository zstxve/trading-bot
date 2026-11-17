# RUNBOOK — operating the bot day to day

GUIDE.md explains *what* the system is; this file is *how you run it*.
Current setup: **paper trading `breakout_donchian` on 1d bars** (`config_daily.yaml`),
validation clock started **2026-07-10**, go/no-go review earliest **2026-10-09**.

Everything below assumes:

```bash
cd ~/TradingBot
source .venv/bin/activate
```

> **Why the project lives in `~/TradingBot` and not `~/Documents`**: macOS
> privacy protection (TCC) silently refuses background execution — launchd
> agents exit 78, cron jobs never fire — for anything inside Documents/
> Desktop/Downloads. Days were lost to this. Do not move it back.

---

## Daily routine (30 seconds)

1. Open **http://127.0.0.1:8791/** (bookmark it — live, self-refreshing;
   fallback if the server is down: `open state/dashboard.html`)
2. Look at three things:
   - **SUPERVISION** card — green `supervised (launchd)` is healthy; amber
     `unsupervised/interim` = works but won't survive a crash; red
     `NOT RUNNING` = start it (below)
   - **"what to improve next"** — anything red?
   - **red KILL SWITCH banner** — if present, see incident playbook
3. Done. Don't touch anything else. The bot decides once per day at 00:00 UTC.

## Weekly routine (5 minutes)

```bash
python -m tradingbot report --config config_daily.yaml   # paper vs backtest, same window
```

- Trade counts, average trade, drawdown should roughly match the backtest
  column. The report prints red flags itself.
- Glance at the dashboard checklist: PENDING items should convert to PASS
  over time, never the reverse.
- That's the whole job. Divergence you can't explain = investigate before
  the clock keeps running; do NOT "fix" it by tuning parameters.

## Starting / stopping

| Action | Command |
|---|---|
| Status | http://127.0.0.1:8791/ or `pgrep -fl tradingbot` |
| Start everything (persistent: bot + dashboard, survives reboot & crash) | `./deploy/install_launchd.sh` |
| Start bot only (foreground, for watching) | `python -m tradingbot paper --config config_daily.yaml` |
| Start dashboard only (foreground) | `python -m tradingbot dashboard-server --config config_daily.yaml` |
| Stop everything | `./deploy/uninstall_launchd.sh` (agents) or Ctrl-C (foreground) |
| Refresh candles | `python -m tradingbot fetch-data --config config_daily.yaml` |

## Running it 24/7 on this Mac

Already set up (2026-07-17): both agents installed and running from
`~/TradingBot`. They start at every login and restart on crash.

- Verify anytime: `launchctl list | grep tradingbot` → two lines with PIDs.
- Reinstall after changes: `./deploy/install_launchd.sh`
- **Stop the Mac from sleeping**, or the bot sleeps with it:
  System Settings → Battery/Energy → on power adapter, enable
  *"Prevent automatic sleeping when the display is off"* — then leave the
  Mac plugged in (lid open or an external display attached; a closed-lid
  MacBook without a display sleeps regardless).
- Bookmark http://127.0.0.1:8791/.

Fallback if launchd ever misbehaves again: `./deploy/install_cron.sh`
schedules `deploy/watchdog.sh` every 5 minutes (idempotent; instance lock
prevents duplicates). Remove with `./deploy/uninstall_cron.sh`.

Honest limit: a laptop that gets unplugged, carried around or updated
overnight will still produce gaps (the dashboard counts them). The
gold-standard fix stays a €5/month VPS running the identical repo under
systemd.

Facts that make this safe to do casually:

- **Double-start is impossible** — a second instance exits immediately
  (`state/paper_runner.lock`).
- **State survives restarts** — cash/positions/risk baselines persist in
  `state/paper_state.json`; a restarted bot resumes, it doesn't forget.
- **Stops are enforced** intrabar via a 60s price poll while the loop runs.
- Machine asleep = bot paused. Gaps show up in the dashboard ("missing
  bars"). For a clean validation record run it on an always-on box/VPS.

## Paper-trading workflow (the 90-day validation)

The point: measure whether reality matches the walk-forward result *before*
money is involved. Current reference (dashboard "validated reference"):
Sharpe 1.16, PF ~5, maxDD −12% over ~6y stitched OOS.

1. Leave the loop running. **Expect boredom**: the strategy trades ~2x/month
   per symbol and sat flat through entire bear years in the backtest. Zero
   trades for weeks is on-profile, not broken.
2. Daily/weekly routines above.
3. Every trade, breach and daily summary lands in Telegram once enabled
   (`.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`; config:
   `alerts.telegram_enabled: true`).
4. **Do not change `config_daily.yaml` while the clock runs.** Any change to
   the active strategy's behaviour restarts the 90 days. Experiments go in a
   separate config file (see fine-tuning).
5. Around **2026-10-09**, hold the go/no-go review: every box in the
   dashboard checklist must be green, including the one only you can answer
   (*why* does this edge exist?). Any FAIL/PENDING = extend paper or stop.
   All green = smallest live test (€500–1000), treating the first 3 live
   months as another validation phase.

## Fine-tuning policy (read before touching any parameter)

**The only sanctioned tuning is walk-forward re-optimisation**:

```bash
python -m tradingbot walkforward --config config_daily.yaml                     # active strategy
python -m tradingbot walkforward --config config_daily.yaml --strategy trend_ma_cross
```

It picks parameters on a 1-year window, trades them blind for 4 months,
rolls forward — parameters are *outputs* of the process, and only the
stitched test segments count. Reports land in `reports/walkforward_*/` with
metadata; the dashboard automatically uses the newest report matching the
active strategy+timeframe.

Rules that keep the numbers honest:

- Grids live in `config_daily.yaml → walkforward.grids`. **Never widen a
  grid because results disappoint** — that is curve-fitting with extra steps.
- Judge a parameter set by *stability across windows* (dashboard shows
  `distinct sets chosen`), not by the best single window.
- Manual parameter fiddling against the backtest = overfitting = the reason
  most published backtests are fiction. The system will happily let you do
  it; it will also happily lose the money later.
- Experiments run from a **copy** of the config
  (`cp config_daily.yaml config_experiment.yaml`, edit, run backtest/
  walkforward with `--config config_experiment.yaml`) so the live paper
  session is untouched.

Strategy status (walk-forward on 1d, 8y history, as of 2026-07-16):

| strategy | verdict | note |
|---|---|---|
| `breakout_donchian` | **active** — only go/no-go passer | Sharpe 1.16, PF ~5, stable params; avoid `exit_n ≥ 25` (holds through crashes) |
| `trend_ma_cross` | viable but weaker (Sharpe 0.99, PF 1.97) | retuned 2026-07-16: base now fast 10 / slow 50 / mult 2 (dominant WF pick); comparison baseline |
| `meanrev_rsi_bb` | **dead on 1d**: 21/21 windows found no qualifying params, zero trades; failed on 1h too | research shelf — needs a different regime idea, not new parameters |

## Incident playbook

| Symptom | Action |
|---|---|
| Dashboard: `NOT RUNNING` | `./deploy/install_launchd.sh` (or foreground start). Check `logs/launchd.err.log` / `logs/nohup.out.log` for why it died. |
| launchd shows `78` after login | macOS background-item state is wedged: reboot. Still 78 → System Settings → Login Items → allow the python item, then `launchctl kickstart gui/$(id -u)/com.tradingbot.paperloop`. |
| Red KILL SWITCH banner | Read the reason in the banner/`state/KILL_SWITCH`. Investigate root cause. Resume = delete the file (runner rechecks every 60s). |
| Kill switch was a **max-drawdown** breach | Deleting the file is not enough — the persisted peak re-trips it by design. A 15% drawdown means stop-and-rethink: archive `state/` for a genuinely fresh session, and treat the strategy as failed validation. |
| `stale data` kill | Exchange/network outage or the machine slept through bars. Fix connectivity, verify candles arrive (`fetch-data`), delete the kill file. |
| Missing bars warning | Machine slept. Cosmetic for the strategy (state resumes) but weakens the validation record — move to an always-on box. |

## Going live (October, only if all boxes green)

Gates and steps in GUIDE.md → "Going live". Before first real order, additionally
build (deliberately not built yet): exchange-resident stop orders, Binance
lot-size/precision handling, position reconciliation. Ask for them when — and
only when — the paper phase has earned it.

**1h re-test (2026-07-19, `config_1h_research.yaml`, long lookbacks):** breakout
168h-channel Sharpe 0.31 / PF 1.14 — survived the bear (+4.8% vs benchmark −40%)
but far under the go/no-go bar, parameters unstable 6/9; trend dead at any EMA
length (−43%). **1h stays closed; the daily session remains the only validated
deployment.**
