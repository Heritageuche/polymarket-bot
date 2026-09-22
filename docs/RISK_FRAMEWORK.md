# Risk framework (derived, not chosen)

Bankroll at start: **$103.60**. Contracts: Polymarket Up/Down binaries, price `p` per share, pay `1` if right.
Fees: makers pay nothing; takers pay `0.07 · p · (1-p)` USDC per share (1.75¢ at p = 0.50, i.e. 3.5% of
notional). Exchange minimum: 5 shares per order.

## 1. Stake per trade: fractional Kelly with three multipliers

Full Kelly for a binary at all-in cost `c` (price + taker fee, if any) with win probability `q`:

    f* = (q − c) / (1 − c)

We never stake `f*`. The stake is `f = min( k_dd · shrink · f*, cap )` where:

| term | formula | value at start |
|---|---|---|
| `k_dd` drawdown multiplier | `k = 2 / (1 + ln P / ln x)` from `P(ever drawing down x) = x^(2/k−1)` under fractional-Kelly betting. Config: x = 50 %, P = 5 %. | **0.376** |
| `shrink` estimation-error shrinkage | `e² / (e² + s²)` with `e = q − c` and `s = sqrt(q(1−q)/(30 + n) + 0.05²)`; `n` = resolved trades the calibration has seen. | 0.38 at n = 0 for e = 0.08; → 0.68 at n = 400 |
| `cap` | 20 % of equity per position, 50 % total open exposure, 6 positions | |

Typical entry (q = 0.58 at p = 0.50, maker): full Kelly 16 % → **2.3 % of equity ≈ $2.34 → rounded to the
5-share minimum ($2.50)** only because that minimum is still a positive-growth bet (checked explicitly:
`q·ln(1+f·b) + (1−q)·ln(1−f) > 0` and `f_min ≤ k_dd·f*`). If the exchange minimum would be a
*negative*-growth bet the trade is skipped, which is the honest consequence of a $100 bankroll.

Why a 50 %-drawdown / 5 % budget: with fractional Kelly the drawdown distribution is closed-form and
does not depend on the edge size, so this is the one knob that maps directly to "probability of
survival". Raising `drawdown_prob` to 20 % gives k = 0.60 (more aggressive); the config exposes both.

## 2. There is no fixed per-trade stop-loss

A binary contract cannot lose more than its stake, and the stake is already the Kelly loss budget.
A "−10 % ⇒ exit" rule sells a claim for less than it is worth unless the price move carried
information about the outcome. So mid-life exits are decided by comparing expected log wealth:

    G(φ) = q·ln(W + φ·s·b + (1−φ)·s) + (1−q)·ln(W + φ·s·b)

(W = wealth outside the position, s = shares, b = net sale price, φ = fraction sold, q = *current*
win probability from the model, which already includes price move, remaining time, volatility and
momentum). We sell/hedge the φ that maximises G, only if the gain beats a hysteresis threshold.
Hedging (buying the opposite token at net ask a') is a sale at 1 − a', and the better of the two is used.
This reproduces the operator's manual behaviour where it is +EV (a red position whose bid now exceeds
our q, or a position that has become too large a share of wealth) and refuses it where it is −EV
(a red position the market has *over*-punished). `polybot report` replays every recorded price path
and shows whether fixed −5/−10/−20/−30 % rules would have beaten what the bot did.

## 3. Consecutive losses and daily halts

With P(loss) ≈ 0.42 per trade and ~20 trades/day, a run of 5 straight losses is expected most months
(≈ 79 %), 8 straight ≈ 7 %. At the derived stake, 8 straight losses cost ≈ 17 % of bankroll, 10 ≈ 20 %.
Those are the "possibility of consecutive losses" numbers the sizing is built to survive.

Daily halts stop **new entries only**:

* **Model-failure test**: after ≥ 5 resolved trades, halt when today's realised P/L is 2.5σ below the
  model's own expectation (Σ EV, Σ variance of today's trades). A loss that is merely unlucky does not
  trigger it; a loss inconsistent with the model does.
* **Hard floor**: −8 % of start-of-day equity (3σ of a typical day under the model, clamped to 8–25 %).
* **Daily target**: +10 % of start-of-day equity, latched for the day. It only flips one boolean; position
  management, journaling, shadow signals and learning continue unchanged.

## 4. What is honest about the goal

$103.60 → $100,000 in 30 days is ×965, i.e. **+25.8 % compounded every day**. The +10 %/day rule you
specified compounds to ×17.4 in 30 days (≈ $1,800) even if hit every single day. The bot targets the
rule you gave; it cannot make the two goals consistent, and the Kelly sizing above will not pretend to.
