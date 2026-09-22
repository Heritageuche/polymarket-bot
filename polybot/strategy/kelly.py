"""Kelly sizing and the risk framework derived from it. See docs/RISK_FRAMEWORK.md for the derivation."""
from __future__ import annotations
import math
from dataclasses import dataclass


def kelly_fraction(q: float, cost: float) -> float:
    """Growth-optimal fraction of bankroll to stake on a binary contract that costs `cost`
    per share (fees included) and pays 1 if it wins with probability q.  f* = (q - c) / (1 - c)."""
    if cost <= 0 or cost >= 1 or q <= cost:
        return 0.0
    return (q - cost) / (1.0 - cost)


def drawdown_multiplier(x: float, p: float) -> float:
    """Fraction k of full Kelly such that P(ever drawing down to fraction x of the peak) == p.
    Under continuous fractional-Kelly betting P(DD >= x) = x^(2/k - 1)  =>  k = 2 / (1 + ln p / ln x)."""
    return 2.0 / (1.0 + math.log(p) / math.log(x))


def uncertainty_shrink(edge: float, q_std: float) -> float:
    """Shrinkage for estimation error in q: with edge ~ N(edge_hat, s^2) the optimal stake scales by
    edge^2 / (edge^2 + s^2) (James-Stein-style; equals 1/2 when the error is as large as the edge)."""
    if edge <= 0:
        return 0.0
    return edge * edge / (edge * edge + q_std * q_std)


N_PRIOR = 30   # the untrained model is trusted as much as ~30 resolved trades would justify


def q_std(q: float, n_eff: int, floor: float) -> float:
    """Std-dev of our probability estimate: binomial sampling error from (prior + n_eff) calibration
    trades plus an irreducible model-error floor. Shrinks toward `floor` as evidence accumulates."""
    n = N_PRIOR + max(0, n_eff)
    return math.sqrt(q * (1 - q) / n + floor * floor)


def expected_log_growth(q: float, cost: float, f: float) -> float:
    if f <= 0 or f >= 1:
        return 0.0 if f <= 0 else -1e9
    b = (1 - cost) / cost
    return q * math.log(1 + f * b) + (1 - q) * math.log(1 - f)


@dataclass
class SizeDecision:
    stake_usd: float
    shares: float
    fraction: float
    f_full: float
    f_shrunk: float
    k_dd: float
    edge: float
    growth: float
    reason: str


def size_position(q: float, price: float, fee_per_share: float, equity: float, cfg, n_eff: int,
                  current_exposure_frac: float, min_shares: float) -> SizeDecision:
    cost = price + fee_per_share
    edge = q - cost
    f_full = kelly_fraction(q, cost)
    k_dd = drawdown_multiplier(cfg.drawdown_level, cfg.drawdown_prob)
    s = q_std(q, n_eff, cfg.model_uncertainty_floor)
    f_shrunk = f_full * uncertainty_shrink(edge, s)
    f = min(k_dd * f_shrunk, cfg.max_fraction_per_trade)
    f = min(f, max(0.0, cfg.max_total_exposure - current_exposure_frac))
    stake = f * equity
    shares = math.floor(stake / cost) if cost > 0 else 0
    reason = "ok"
    if edge < cfg.min_edge:
        return SizeDecision(0, 0, 0, f_full, f_shrunk, k_dd, edge, 0.0, "edge below min_edge")
    if shares < min_shares:
        # Would the exchange minimum still be a +growth bet? Only then round up to it.
        f_min = min_shares * cost / equity
        g = expected_log_growth(q, cost, f_min)
        if g > 0 and f_min <= cfg.max_fraction_per_trade and f_min <= k_dd * f_full \
                and f_min <= cfg.max_total_exposure - current_exposure_frac + 1e-12:
            shares = min_shares; stake = shares * cost; f = f_min; reason = "rounded up to exchange minimum"
        else:
            return SizeDecision(0, 0, 0, f_full, f_shrunk, k_dd, edge, g, "below exchange minimum after shrinkage")
    growth = expected_log_growth(q, cost, f)
    return SizeDecision(shares * cost, shares, f, f_full, f_shrunk, k_dd, edge, growth, reason)


# ---------------------------------------------------------------------------- framework printout

def framework_report(cfg, bankroll: float, q_typ: float = 0.58, price_typ: float = 0.50, fee_rate: float = 0.07,
                     trades_per_day: int = 20, n_eff: int = 0) -> str:
    fee = fee_rate * price_typ * (1 - price_typ)
    cost_taker = price_typ + fee
    k = drawdown_multiplier(cfg.drawdown_level, cfg.drawdown_prob)
    s = q_std(q_typ, n_eff, cfg.model_uncertainty_floor)
    lines = [f"Bankroll: ${bankroll:,.2f}",
             f"Drawdown target: P(ever losing {cfg.drawdown_level:.0%}) <= {cfg.drawdown_prob:.0%}  ->  Kelly multiplier k = {k:.3f}",
             "", "Per-trade sizing at a typical opportunity (q = %.2f, price = %.2f):" % (q_typ, price_typ)]
    for label, cost in (("maker (no fee)", price_typ), ("taker (fee %.4f/share)" % fee, cost_taker)):
        f_full = kelly_fraction(q_typ, cost)
        f_sh = f_full * uncertainty_shrink(q_typ - cost, s)
        f = min(k * f_sh, cfg.max_fraction_per_trade)
        lines.append(f"  {label:26s} edge={q_typ-cost:+.3f} full-Kelly={f_full:.3f}  shrunk={f_sh:.3f}  "
                     f"x k -> {f:.3f} = ${f*bankroll:.2f}  ({math.floor(f*bankroll/cost)} shares)")
    f_typ = min(k * kelly_fraction(q_typ, price_typ) * uncertainty_shrink(q_typ - price_typ, s), cfg.max_fraction_per_trade)
    lines += ["", "Consecutive losses (P(loss) = %.2f per trade):" % (1 - q_typ)]
    for n in (3, 5, 8, 10):
        p_run = 1 - (1 - (1 - q_typ) ** n) ** max(1, trades_per_day * 30 // n)
        lines.append(f"  {n:2d} straight losses: bankroll x{(1-f_typ)**n:.3f}   P(at least one such run in a month of {trades_per_day}/day) ~ {min(1,p_run):.0%}")
    # daily loss halt
    stake = f_typ * bankroll
    var_trade = (stake / price_typ) ** 2 * q_typ * (1 - q_typ)
    sd_day = math.sqrt(trades_per_day * var_trade)
    ev_day = trades_per_day * stake * (q_typ / price_typ - 1)
    hard = min(cfg.daily_hard_loss_cap, max(cfg.daily_hard_loss_floor, 3 * sd_day / bankroll))
    lines += ["", f"Daily model expectation at {trades_per_day} trades: EV ${ev_day:+.2f}, 1-sigma ${sd_day:.2f}",
              f"  halt new entries when today's P/L is {cfg.daily_loss_z} sigma below expectation (after {cfg.daily_loss_min_trades} trades)",
              f"  hard floor: halt at -{hard:.1%} of start-of-day equity (= 3 sigma, clamped to [{cfg.daily_hard_loss_floor:.0%}, {cfg.daily_hard_loss_cap:.0%}])",
              "", "Why there is no fixed % stop-loss per trade: a binary contract's maximum loss IS the stake, and the",
              "stake is already the Kelly-derived loss budget. Mid-life exits are handled by the log-utility exit",
              "policy (strategy/exit_policy.py), which sells only when doing so raises expected log growth."]
    return "\n".join(lines)
