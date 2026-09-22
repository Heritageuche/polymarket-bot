"""Probability that an Up/Down window resolves Up.

Model: the resolution value (Chainlink TWAP over the last `twap_lookback` seconds, or the Binance
candle close) is compared with the window open K. With current price S and remaining time tau, the
log-return to resolution is ~ N(mu, sigma^2 * tau_eff). P(Up) = Phi((ln(S/K) + mu) / sqrt(var)).

The raw model probability is then passed through a *learned* logistic layer that blends it with the
market's own price and a momentum feature (see learning/calibration.py). The learned layer starts at
an equal-weight blend so a fresh bot is deliberately humble about its own model.
"""
from __future__ import annotations
import math
from dataclasses import dataclass, asdict
from typing import Optional
from ..polymarket.gamma import Market, CHAINLINK_SYMBOL, BINANCE_SYMBOL
from ..market_data.feed import Feed, MAX_PRICE_AGE


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def logit(p: float) -> float:
    p = min(1 - 1e-6, max(1e-6, p))
    return math.log(p / (1 - p))


def sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


@dataclass
class ModelParams:
    """Learned parameters (persisted & versioned by the learning module)."""
    a0: float = 0.0        # intercept
    a_model: float = 0.5   # weight on logit(q_model)
    a_market: float = 0.5  # weight on logit(p_market)
    a_mom: float = 0.0     # weight on momentum z-score
    vol_mult: float = 1.0  # multiplier on sigma (learned: >1 if the model is overconfident)
    n_fit: int = 0         # resolved trades the current parameters were fitted on

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Estimate:
    q: float                  # final calibrated probability of Up
    q_model: float            # raw model probability
    p_market: float           # market mid for Up
    spot: float
    strike: float
    sigma_per_sec: float
    tau_eff: float
    mom_z: float
    z: float
    strike_uncertainty: float
    features: dict


class ProbabilityModel:
    def __init__(self, feed: Feed, params: ModelParams):
        self.feed = feed
        self.params = params

    def estimate(self, m: Market, p_market: float, now: float) -> Optional[Estimate]:
        cl = CHAINLINK_SYMBOL.get(m.asset); bn = BINANCE_SYMBOL.get(m.asset)
        s = self.feed.series(m.resolution, cl, bn)
        if s is None or s.price() is None:
            return None
        spot = s.price()
        # A dead feed leaves a plausible-looking but OLD price in the series. Pricing a 5-minute
        # market off a stale spot is how a bot confidently takes the wrong side, so refuse instead.
        age = now - s.last_ts
        if age > MAX_PRICE_AGE:
            return None
        strike, k_unc = self.feed.strike(s, m.start_ts, bn)
        if strike is None:
            return None
        if m.start_ts > now:
            return None          # window not open yet: no strike exists
        sig = self.feed.vol_per_sec(s, bn)
        if sig is None or sig <= 0:
            return None
        sig *= self.params.vol_mult
        remaining = m.end_ts - now
        # TWAP over the last L seconds ~ price at end - L/2 (variance of a TWAP is ~1/3 of a point price
        # over the averaging window; the half-window shift captures the bulk of it).
        tau_eff = max(1.0, remaining - m.twap_lookback / 2.0)
        var = sig * sig * tau_eff + k_unc * k_unc
        mom_r = s.log_return(60.0) or 0.0
        mom_z = mom_r / (sig * math.sqrt(60.0)) if sig > 0 else 0.0
        mom_z = max(-4.0, min(4.0, mom_z))
        z = math.log(spot / strike) / math.sqrt(var)
        q_model = phi(z)
        q_model = min(0.995, max(0.005, q_model))
        x = self.params.a0 + self.params.a_model * logit(q_model) + self.params.a_market * logit(p_market) \
            + self.params.a_mom * mom_z
        q = sigmoid(x)
        q = min(0.99, max(0.01, q))
        feats = {"logit_model": logit(q_model), "logit_market": logit(p_market), "mom_z": mom_z,
                 "tau_eff": tau_eff, "abs_z": abs(z), "sigma": sig, "interval": m.interval, "asset": m.asset}
        return Estimate(q, q_model, p_market, spot, strike, sig, tau_eff, mom_z, z, k_unc, feats)
