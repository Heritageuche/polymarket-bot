"""Hold / sell / hedge decisions for an open position, made the Kelly way.

With cash W (everything except this position), s shares, a net sale price b (bid minus taker fee, or
the maker price we could realistically post), and current win probability q, selling a fraction phi
gives expected log wealth

    G(phi) = q * ln(W + phi*s*b + (1-phi)*s) + (1-q) * ln(W + phi*s*b)

We pick the phi that maximises G, and only act if the improvement over holding beats a small
hysteresis threshold (so we don't churn through fees).  A hedge (buying the opposite token at net
ask a') is economically a sale at price (1 - a'), so we compare and use whichever is better.

Properties that fall out of this, matching the behaviour the operator observed by hand:
  * A position that is losing because the price moved against it has a *lower q*. If the market's bid
    is now above what our model thinks the claim is worth, selling is +EV and we sell. If the market
    bid is below our q, holding is +EV and we hold even though we are red.  "-10% => exit" is therefore
    never applied blindly: it only triggers when the drop reflects a real drop in win probability that
    the bid has not fully priced.
  * A large position relative to bankroll gets partially sold even at a slightly unfavourable price,
    because log utility penalises the variance of concentrated bets (this is Kelly re-balancing).
  * A green position with q near 1 is almost never sold: its payoff is nearly certain and the bid is
    below 1, so G(0) beats every G(phi > 0).
"""
from __future__ import annotations
import math
from dataclasses import dataclass


@dataclass
class ExitDecision:
    action: str            # 'hold' | 'sell' | 'hedge'
    fraction: float        # of the position to sell / hedge
    shares: float
    price: float           # net price per share we expect to realise (sell) or pay (hedge)
    gain: float            # improvement in expected log growth vs holding
    g_hold: float
    g_best: float
    reason: str


def growth(q: float, W: float, s: float, b: float, phi: float) -> float:
    W = max(W, 1e-6)
    win = W + phi * s * b + (1 - phi) * s
    lose = W + phi * s * b
    if win <= 0 or lose <= 0:
        return -1e9
    return q * math.log(win) + (1 - q) * math.log(lose)


def decide(q: float, cash: float, shares: float, sell_net: float | None, hedge_net_cost: float | None,
           min_shares: float, min_improvement: float, all_or_none: bool = False) -> ExitDecision:
    g0 = growth(q, cash, shares, 0.0, 0.0)
    candidates = []
    if sell_net is not None and sell_net > 0:
        candidates.append(("sell", sell_net))
    if hedge_net_cost is not None and 0 < hedge_net_cost < 1:
        candidates.append(("hedge", 1.0 - hedge_net_cost))
    best = ExitDecision("hold", 0.0, 0.0, 0.0, 0.0, g0, g0, "holding is growth-optimal")
    for action, b in candidates:
        fracs = [1.0] if all_or_none else [i / 20 for i in range(1, 21)]
        for phi in fracs:
            n = phi * shares
            if n < min_shares and phi < 1.0:
                continue            # exchange minimum for partial orders
            if phi < 1.0 and (shares - n) < min_shares:
                continue            # don't leave a dust remainder
            g = growth(q, cash, shares, b, phi)
            if g - g0 > best.gain:
                px = b if action == "sell" else 1.0 - b
                best = ExitDecision(action, phi, n, px, g - g0, g0, g,
                                    f"{action} {phi:.0%}: expected log growth +{g-g0:.5f}")
    if best.gain < min_improvement:
        return ExitDecision("hold", 0.0, 0.0, 0.0, best.gain, g0, best.g_best,
                            "improvement below hysteresis threshold" if best.gain > 0 else "holding is growth-optimal")
    return best
