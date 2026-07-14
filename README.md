# TradingBot

A paper-trading-first crypto trading system for Binance spot markets: event-driven backtesting, walk-forward validation, volatility-based position sizing, hard risk limits, and a kill switch. Live trading exists but is **disabled by default behind three independent gates**.

> **No profit claims.** Nothing here guarantees — or even suggests — positive returns. Most systematic retail strategies do not survive costs. The purpose of this project is to *measure honestly* whether a strategy has any edge and to *cap the damage* while you find out. Expect live results to be worse than out-of-sample backtests, which will be worse than in-sample backtests. That ordering is close to a law of nature.

## What it trades (default configuration)

| Setting | Value |
|---|---|
| Market | Binance spot (long-only, no leverage) |
| Assets | BTC/USDT, ETH/USDT, SOL/USDT, BNB/USDT |
| Signals | 1h bars, with a causal 4h regime filter |
| Simulated capital | $10,000 |
| Risk per trade | 0.5% of equity (entry-to-stop distance) |
| Hard max drawdown | 15% → kill switch, everything flattens |
| Daily loss limit | 2% → no new entries until next UTC day |
| Costs assumed | 10 bps fee + 5 bps slippage + 2 bps half-spread per side |

## Project layout

```
TradingBot/
├── config.yaml               # all tunables (validated strictly at startup)
├── .env.example              # secrets template — copy to .env, never commit
├── requirements.txt
├── tradingbot/
│   ├── cli.py                # entrypoints: fetch-data / backtest / walkforward / paper / live
│   ├── config.py             # pydantic config schema + validation
│   ├── costs.py              # fee / slippage / spread model
│   ├── pipeline.py           # per-bar decision step SHARED by backtest and live
│   ├── portfolio.py          # cash, positions, trade accounting
│   ├── logging_setup.py      # structured logging (console + JSON-capable file)
│   ├── data/
│   │   ├── loader.py         # Binance OHLCV download + CSV cache + synthetic generator
│   │   └── resample.py       # CAUSAL 4h→1h alignment (the anti-look-ahead core)
│   ├── strategies/
│   │   ├── base.py           # Strategy interface (signal-only; never fills itself)
│   │   ├── indicators.py     # causal EMA/SMA/RSI/ATR/Bollinger
│   │   ├── trend_ma_cross.py # EMA cross + vol filter + 4h regime
│   │   └── meanrev_rsi_bb.py # RSI/Bollinger dip-buy gated by 4h trend
│   ├── risk/
│   │   ├── sizing.py         # volatility (stop-distance) based position sizing
│   │   └── limits.py         # drawdown/daily/exposure limits + kill switch
│   ├── execution/
│   │   ├── base.py           # BrokerAdapter interface, Order/Fill types
│   │   ├── validation.py     # every order passes here before execution
│   │   ├── paper.py          # simulated fills (default venue)
│   │   ├── binance_live.py   # real orders — triple-gated, off by default
│   │   └── retry.py          # exponential backoff for API calls
│   ├── backtest/
│   │   ├── engine.py         # event-driven engine (next-bar-open execution)
│   │   ├── metrics.py        # CAGR/Sharpe/Sortino/DD/PF/... + benchmark
│   │   ├── walkforward.py    # rolling optimise→test→roll, OOS stitching
│   │   └── plots.py          # equity/drawdown/monthly/trade-distribution PNGs
│   └── live/runner.py        # paper/live loop: state persistence, stop polling,
│                             #   graceful shutdown, kill-switch enforcement
└── tests/                    # unit + integration tests (pytest)
```

## Setup

```bash
cd TradingBot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # keys only needed for LIVE trading, not paper
pytest                        # everything should pass before you trust anything
```

## Commands

```bash
# 1. Download/refresh 2 years of 1h candles (public endpoints, no key needed)
python -m tradingbot fetch-data

# 2. Backtest: in-sample + 20% out-of-sample holdout, vs buy & hold benchmark
python -m tradingbot backtest
python -m tradingbot backtest --strategy meanrev_rsi_bb
python -m tradingbot backtest --synthetic          # offline demo on synthetic data

# 3. Walk-forward: optimise on 6 months, trade 2 months forward, roll, repeat
python -m tradingbot walkforward                    # takes several minutes

# 4. Paper trading (DEFAULT mode): real market data, simulated fills
python -m tradingbot paper

# 5. LIVE trading — disabled by default. Requires ALL THREE gates:
#      config.yaml:  live.enabled: true
#      environment:  TRADINGBOT_LIVE_ACK=I-UNDERSTAND-LIVE-TRADING-RISKS-AND-ACCEPT-LOSSES
#      CLI flag:     --i-really-want-live
python -m tradingbot live --i-really-want-live

# 6. Ops: divergence report + read-only status page
python -m tradingbot report      # paper vs backtest over the same dates
python -m tradingbot dashboard   # writes state/dashboard.html (also auto-updated hourly)
```

Reports (metrics JSON, trades CSV, PNG charts) land in `reports/<run_id>/`. Logs in `logs/`. Paper state and the kill-switch file in `state/`.

## The two strategies

**`trend_ma_cross`** — long when the 20-EMA is above the 50-EMA on 1h bars, price is above the 50-EMA on 4h bars, and ATR% of price is inside a sane band (dead chop and panic regimes are both skipped). Initial stop at 3×ATR, trailed chandelier-style. Exits on trend flip, regime loss, or stop. With `allow_short: true` (research only, see assumptions) the logic mirrors to the short side.

**`meanrev_rsi_bb`** — long when RSI(14) < 30 *and* price is below the lower Bollinger band, but **only while price holds above the 4h EMA** — it buys dips inside uptrends and refuses falling knives. Exits on RSI recovery, middle-band tag, 2×ATR stop, or a 48-bar time stop.

**`breakout_donchian`** — turtle-style channel breakout, designed for daily bars (`config_daily.yaml`): long when the close breaks the prior 55-bar high (the breakout bar is excluded from its own channel — `shift(1)`), gated by a slow-EMA regime filter. Exit below the prior 20-bar low or on a 3×ATR stop. Mirrors short with `allow_short`.

All three are deliberately boring, well-known baselines. They are starting points for your own research, not finished products. `config_daily.yaml` runs the same system on 1d bars with 8 years of history — ~20× fewer trades, so costs stop dominating.

## How the system avoids fooling you

- **No look-ahead by construction**: signals computed on bar *t*'s close execute at bar *t+1*'s open. The 4h regime value only becomes visible after the 4h bar closes (`data/resample.py`). `tests/test_no_lookahead.py` proves causality empirically by truncating history and asserting identical signals.
- **Pessimistic fills**: stop and take-profit hit in the same bar → the stop is assumed first. Price gaps through your stop → you fill at the (worse) open, not at your stop price.
- **Costs always on**: every simulated fill pays fee + slippage + half-spread. The benchmark pays entry costs too.
- **Out-of-sample discipline**: `backtest` holds out the final 20% of history; `walkforward` reports *only stitched test windows* as the headline number, prints the chosen-parameters-per-window table (stable parameters = believable; flip-flopping = noise), and rejects parameter sets with too few trades.
- **One decision pipeline** (`pipeline.py`) shared by backtest and paper/live, so paper trading can't quietly run different logic than what you validated.

## Risk management (enforced, not advisory)

- Position size = (equity × 0.5%) / stop distance — volatile assets automatically get smaller positions. No stop, no trade.
- Per-asset exposure ≤ 25% of equity, total ≤ 80%, max 4 open positions, spot cash only (leverage requires an explicit double opt-in in config).
- Daily loss ≥ 2% → entries halted until the next UTC day. Drawdown ≥ 15% from peak → **kill switch**: flatten everything, halt permanently, write `state/KILL_SWITCH`. The bot refuses to start while that file exists; deleting it is a deliberate human decision.
- Kill switch also engages on 5 consecutive API errors or a stale data feed.
- A halted system still processes **sells** — risk reduction is never blocked.

## Assumptions the system makes (read this)

1. **Fill assumption**: market orders fill fully at reference price ± slippage model. Real books can be thinner; large orders would slip more. Sizes here (fractions of $10k) are small enough for BTC/ETH but revisit for less liquid pairs.
2. **Latency assumption**: one full bar between signal and execution — conservative for 1h bars.
3. **Stops are synthetic**: enforced by the bot (intrabar checks in backtest, ~60s ticker polling live), not resting exchange orders. If the bot process dies, live stops die with it until restart. A production system would mirror stops as exchange stop-limit orders.
4. **Candle data is clean**: Binance history is good but not perfect; the loader drops forming candles and deduplicates, nothing more.
5. **Short selling is simulation-only**: setting `risk.allow_short: true` (plus the strategy's `allow_short`) enables shorts in backtest/paper for research. The model credits proceeds to cash, marks positions with signed quantity, and charges `costs.short_borrow_bps_per_day` carry — but assumes unlimited borrow, no forced liquidation, no funding spikes. Real shorting is harsher. The live spot adapter refuses short-opening orders unconditionally (Binance spot cannot short; shorting live would require a margin/futures adapter that does not exist here).
6. **Walk-forward windows restart flat** (no positions straddle window boundaries) — slightly conservative.
7. **Taxes are your problem**: nothing here models them.

## Biggest failure modes (in rough order of likelihood)

1. **Overfitting** — the reason most backtests are fiction. Every parameter you tune is fitted partly to noise. Defenses here: tiny grids, walk-forward, OOS holdout, parameter-stability reporting. Defense that only you can provide: *stop touching parameters after looking at OOS results*. Each peek at OOS data spends it.
2. **Regime change** — a strategy validated on 2 years of data learned those 2 years. Crypto flips between trending and chopping; a trend system bleeds in chop through many small stop-outs. The regime/vol filters reduce but don't eliminate this.
3. **Underestimated costs** — at 1h frequency the assumed ~34 bps round trip is plausible for majors; if your paper fills consistently look better than live tickers would suggest, raise `slippage_bps` and re-run everything.
4. **Correlation collapse** — BTC/ETH/SOL/BNB routinely move as one asset in crashes. Four "diversified" positions can behave like one 4× position. The total-exposure cap and portfolio-level drawdown limit are sized for exactly this.
5. **Operational failures** — exchange outages, API bans, clock drift, the bot dying with positions open. Mitigations: retries, stale-data kill switch, state persistence, graceful shutdown. Not bulletproof.
6. **Software bugs** — a sign error in sizing is worth more damage than a bad strategy. That is why risk rules, sizing, validation and engine mechanics have dedicated tests, and why live orders re-validate everything.
7. **Black swans** — flash crashes gap through stops (the backtest models gap fills pessimistically, reality can be worse); stablecoins can depeg; exchanges can freeze withdrawals. Position sizing is the only defense that always works: never fund an account with money you can't lose.

## Running paper 24/7 (macOS)

```bash
./deploy/install_launchd.sh     # start at login, restart on crash
launchctl list | grep tradingbot
./deploy/uninstall_launchd.sh   # graceful stop (state persisted)
```

launchd restarts the process but cannot keep a sleeping laptop awake — for a real multi-month run use a small VPS (any always-on Linux box: same code, use systemd or `nohup` + cron) or prevent sleep explicitly. The runner persists state after every cycle and resumes on restart, so restarts are safe.

**Alerts**: create a Telegram bot (@BotFather), put `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`, set `alerts.telegram_enabled: true`. You get: runner start/stop, every closed trade, risk breaches, kill-switch engagement, and a daily summary. Alert delivery failures are logged and never interrupt trading.

**Dashboard**: `state/dashboard.html` — self-contained, read-only, regenerated hourly by the runner (or on demand via `python -m tradingbot dashboard`). Deliberately has no buttons: the kill switch stays a file.

## Paper-trade protocol (before any real money)

1. Run `backtest` and `walkforward` on both strategies. Read the OOS numbers only.
2. Run `python -m tradingbot paper` continuously (see "Running paper 24/7") for **at least 3 months or 100 trades, whichever comes later**.
3. Weekly, run `python -m tradingbot report` — it backtests the same config over the paper session's exact date range and prints paper vs backtest side by side with divergence red flags (trade count, average trade, drawdown).
4. Kill-switch drills: touch `state/KILL_SWITCH` while it runs; verify it refuses to trade (the process stays alive and rechecks every 60s). Kill the process mid-position; verify restart resumes state correctly.
5. **Resetting after a kill**: deleting `state/KILL_SWITCH` resumes the loop, but after a *max-drawdown* kill the persisted peak equity re-trips the limit on the next bar. That is intentional — a 15% drawdown is a stop-and-rethink event, not a restart event. To truly restart: archive `state/` and begin a fresh session (new baseline, new clock).
6. Log every manual intervention. If you keep overriding the bot, you don't have a system.

## Go/no-go checklist for considering live testing

Consider a *small* live test only if **all** of these hold on out-of-sample / paper results:

- [ ] OOS Sharpe > 1.0 **and** OOS profit factor > 1.3 (after costs)
- [ ] OOS max drawdown < your 15% limit with margin (e.g. < 10%)
- [ ] ≥ 100 OOS/paper trades (fewer = you know nothing statistically)
- [ ] Walk-forward parameters stable across windows (no flip-flopping)
- [ ] Strategy beats buy & hold on **risk-adjusted** terms (Sharpe/Sortino, drawdown) — raw return is allowed to be lower
- [ ] 3+ months of paper trading matching backtest expectations
- [ ] You can explain in one sentence *why* the edge should exist and *who* is on the other side of your trades

If any box is unchecked: do not go live. If all are checked: start with money sized so that the full 15% drawdown is boring — e.g. $500–$1000, not $10k — and treat the first 3 live months as another (paid) validation phase, comparing live fills against paper assumptions.

## Going live (deliberately annoying)

1. Complete the checklist above. 2. Create Binance API keys: **withdrawals disabled, IP-restricted, spot only**; put them in `.env`. 3. Set `live.enabled: true` in config.yaml. 4. Set `TRADINGBOT_LIVE_ACK` to the exact string in `.env.example`. 5. Run with `--i-really-want-live`. Miss any gate and the live broker refuses to construct — by design. Before production use, also add exchange lot-size/precision handling and order reconciliation in `binance_live.py` (marked in the file).

## Extending

- New strategy: subclass `Strategy` in `tradingbot/strategies/`, register it in `registry.py`, add a params block to config + a grid to `walkforward.grids`. The no-lookahead test parametrization is the first thing to extend.
- New venue: implement `BrokerAdapter` (`execution/base.py`).
- Ideas worth more than parameter tuning: portfolio of both strategies with allocation, funding-aware perp support, exchange-resident stop orders, regime classification.
