# polybot — Kelly-sized Up/Down trading on Polymarket crypto markets

Trades Bitcoin / ETH / SOL / XRP "Up or Down" markets (5m, 15m, 1h, 4h) with Kelly-criterion sizing,
a log-utility exit policy, daily target/loss halts, a full trade journal and a guarded learning loop.
Read `docs/RISK_FRAMEWORK.md` before running it.

> **⚠️ No stop limits in v1.** This release has **no stop-loss, no stop-limit and no per-trade
> stop orders**. A position is sized by Kelly and then held to resolution unless the log-utility
> exit policy says selling raises expected growth — the maximum loss on any single trade is the
> stake. The only automatic halts are at the *day* level (+10 % target latch, model-failure loss
> halt, hard floor) and the manual `pause` / `stop` / `kill` controls below. **Stop limits are
> planned for v2** — see [Roadmap](#roadmap).

## Setup

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt
cp .env.example .env      # then paste your key + funder address yourself (never share them)
.venv/bin/python -m pytest -q
```

`POLY_FUNDER` is the address shown on your Polymarket profile menu, NOT the address of your signing key.
Polymarket has three wallet generations and the funder + signature_type must match:

| account type | signature_type | funder |
|---|---|---|
| Deposit Wallet (accounts from ~May 2026) | **3** | beacon-proxy address derived from the signer |
| legacy Magic/Google proxy | 1 | CREATE2 proxy address |
| legacy Gnosis Safe (MetaMask/Rabby) | 2 | Safe address |
| plain EOA | 0 | the signer itself |

`polybot check` derives all of these from your key and tells you which one matches, so run it first.
A wrong signature_type makes the balance read **$0.00** even when the account is funded.
Deposit Wallets require py-clob-client-v2; the older py-clob-client only supports types 0-2. The key must be able to sign for that funder (Polymarket: Settings → Export private key).
You must have set USDC allowances once by trading manually on the site.

## Run

```bash
.venv/bin/python -m polybot risk            # the sizing / drawdown numbers for your bankroll
.venv/bin/python -m polybot run --paper     # simulated fills against the live order books (default)
.venv/bin/python -m polybot run --live      # real orders
.venv/bin/python -m polybot status          # heartbeat, equity, day P/L, open positions
.venv/bin/python -m polybot report          # what worked, what failed, calibration, stop-rule study
```

Run paper mode until `report` shows ≥ 40 resolved signals and a calibration table you believe.

## Hitting the daily target

**You do not have to do anything.** At +10% of start-of-day equity the bot latches `target_hit` and
stops opening new positions by itself. Everything else keeps running on purpose:

| keeps running while at target | why |
|---|---|
| open positions are still managed and exited | money already at risk must not be abandoned |
| every opportunity it declines is still journaled as a *shadow signal* | the model must not go blind on good days |
| shadow signals still resolve to win/loss | halted days still produce training rows |
| recalibration still fires (`learn_every_signals`) | the model sharpens on a halted day instead of going stale |
| the price feed, clock sync and status file keep updating | a restart-free resume at midnight |

The learner trains on *resolved signals*, not on resolved trades, which is what makes this work: a day
spent entirely above target still teaches it exactly as much as a day spent trading. The latch clears
at the next trading-day boundary (`day_timezone`), and the new day's +10% is measured from the new,
larger equity. Verified by `tests/test_halt_behaviour.py`.

Only stop the process yourself if you want the machine free. Doing so loses nothing: open trades are
re-attached from the journal on restart.

## Turning it OFF

| command | effect |
|---|---|
| `polybot pause` / `resume` | no new entries; open positions keep being managed |
| `polybot stop` | no new entries; manage open positions to resolution; then the process exits |
| `polybot stop --flatten` | cancel open orders, sell every position at best available, exit |
| `polybot stop --now` | cancel open orders, exit immediately; positions resolve on their own |
| `polybot kill` | `stop --now` **and** create `control/KILL`; the bot refuses to start until `polybot kill --off` |
| Ctrl-C | same as `stop --now`; a second Ctrl-C hard-exits |

All of these only flip flags the engine reads each loop. No strategy code, no model parameter and no
journal row is touched by stopping, so restarts resume cleanly (open trades are re-attached from the journal).

## How it decides

1. **Probability** (`strategy/probability.py`): Chainlink 1-second ticks (the actual resolution source) give
   the window open `K`, spot `S`, and a volatility estimate; `P(Up) = Φ(ln(S/K)/σ√τ)` with TWAP-adjusted τ.
   A learned logistic layer blends that with the market's own price and momentum (starts 50/50).
2. **Sizing** (`strategy/kelly.py`): fractional Kelly with drawdown-derived multiplier and estimation-error
   shrinkage; maker (post-only) orders first because takers pay 3.5 % at the money.
3. **Exit** (`strategy/exit_policy.py`): sell/hedge only when it raises expected log growth.
4. **Day** (`risk/daily.py`): +10 % target latch, model-failure loss halt, hard floor.
5. **Learning** (`learning/`): every taken *and* shadow signal is journaled and resolved; parameters are
   re-fitted with a prior centred on the current values, accepted only on time-ordered hold-out
   improvement, applied 30 % of the way, versioned. Losing buckets (asset/interval/edge/z/time/momentum)
   are blocked once their optimistic ROI bound is negative.

## Known limits

* Hourly markets resolve on Binance candles (handled); 5m/15m/4h on Chainlink TWAP (handled via the
  RTDS feed; strike falls back to the Binance 1-minute open with extra uncertainty if the bot was not
  running at window start).
* Winning shares on resolved markets are settled by Polymarket; the bot does not send on-chain redeem
  transactions. Check the site if `positions` shows `redeemable` items accumulating.
* The first 90 s after start are warm-up (no entries) while the feed backfills; exits are only taken when they
  remain +EV with the model's probability shifted one sigma in the position's favour (anti-churn).
* The host clock is not trusted: all market timing uses exchange time (`polybot/clock.py`).
* Paper fills for resting maker orders are a heuristic (fill when the book trades through, or after 8 s at
  the touch). Treat paper P/L as optimistic.

## Roadmap

### v2 — stop limits

v1 ships without them on purpose (see `docs/RISK_FRAMEWORK.md` §2: on a binary contract the
maximum loss *is* the stake, so a naive price stop mostly just realises noise). v2 adds explicit,
configurable stop behaviour on top of the existing exit policy:

* per-trade stop-limit orders with a configurable trigger and limit offset
* trailing stops on open positions, measured in model probability rather than raw price
* per-bucket stop rules (asset / interval / time-of-day) driven by the calibration study
* a hard per-trade loss cap that overrides the log-utility exit

Until then, treat the day-level halts and the manual controls as the only stops.
