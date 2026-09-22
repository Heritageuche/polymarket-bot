"""Controlled learning.

The trainable object is the logistic layer in ProbabilityModel:
    logit(q) = a0 + a_model*logit(q_model) + a_market*logit(p_market) + a_mom*mom_z
plus a volatility multiplier. Training data = every *resolved* signal (taken or shadow), so the
daily-target halt does not starve the learner: it keeps seeing outcomes for the signals it would
have traded.

Guard rails:
  * nothing is fitted before `learn_min_trades` resolved samples;
  * the fit is a MAP estimate with a prior centred on the CURRENT parameters (ridge toward what we
    already believe) so a handful of outliers cannot swing it;
  * a candidate is accepted only if it beats the current parameters on a time-ordered hold-out
    (last 30%) by log-loss; ties/losses are rejected;
  * even an accepted candidate is applied only part of the way (`learn_max_step`), and every
    version is stored so any update can be audited or rolled back.
"""
from __future__ import annotations
import json
import math
from dataclasses import dataclass
from typing import List, Optional, Tuple
import numpy as np
from ..strategy.probability import ModelParams, logit

FEATS = ("logit_model", "logit_market", "mom_z")


def _design(rows) -> Tuple[np.ndarray, np.ndarray]:
    X, y = [], []
    for r in rows:
        f = json.loads(r["features"]) if isinstance(r["features"], str) else (r["features"] or {})
        if not all(k in f for k in FEATS):
            continue
        X.append([1.0, f["logit_model"], f["logit_market"], f["mom_z"]])
        y.append(1.0 if r["won"] else 0.0)
    return np.array(X), np.array(y)


def _fit_map(X, y, prior: np.ndarray, lam: float, iters: int = 200) -> np.ndarray:
    """Newton's method for ridge logistic regression penalised toward `prior`."""
    w = prior.copy()
    n = len(y)
    for _ in range(iters):
        z = X @ w
        p = 1 / (1 + np.exp(-z))
        g = X.T @ (p - y) / n + lam * (w - prior)
        W = p * (1 - p)
        H = (X.T * W) @ X / n + lam * np.eye(len(w))
        step = np.linalg.solve(H, g)
        w -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return w


def logloss(X, y, w) -> float:
    z = X @ w
    p = np.clip(1 / (1 + np.exp(-z)), 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier(X, y, w) -> float:
    p = 1 / (1 + np.exp(-(X @ w)))
    return float(np.mean((p - y) ** 2))


@dataclass
class FitResult:
    accepted: bool
    note: str
    new_params: Optional[ModelParams]
    n: int
    val_ll_old: float
    val_ll_new: float


def propose_update(rows, current: ModelParams, cfg) -> FitResult:
    X, y = _design(rows)
    n = len(y)
    if n < cfg.learn_min_trades:
        return FitResult(False, f"need {cfg.learn_min_trades} resolved samples, have {n}", None, n, 0, 0)
    cut = int(n * 0.7)
    Xtr, ytr, Xva, yva = X[:cut], y[:cut], X[cut:], y[cut:]
    prior = np.array([current.a0, current.a_model, current.a_market, current.a_mom])
    lam = 5.0 / max(1, cut)          # prior strength decays as evidence accumulates
    w = _fit_map(Xtr, ytr, prior, lam)
    ll_old = logloss(Xva, yva, prior); ll_new = logloss(Xva, yva, w)
    if ll_new >= ll_old - 1e-4:
        return FitResult(False, f"candidate not better on hold-out (old {ll_old:.4f} vs new {ll_new:.4f})", None, n, ll_old, ll_new)
    # partial step toward the accepted candidate
    w_applied = prior + cfg.learn_max_step * (w - prior)
    # sanity bounds: keep weights in a plausible region
    w_applied[1] = float(np.clip(w_applied[1], 0.0, 1.5))
    w_applied[2] = float(np.clip(w_applied[2], 0.0, 1.5))
    w_applied[3] = float(np.clip(w_applied[3], -0.5, 0.5))
    w_applied[0] = float(np.clip(w_applied[0], -0.5, 0.5))
    newp = ModelParams(a0=w_applied[0], a_model=w_applied[1], a_market=w_applied[2], a_mom=w_applied[3],
                       vol_mult=_vol_multiplier(rows, current.vol_mult, cfg), n_fit=n)
    return FitResult(True, f"accepted: hold-out log-loss {ll_old:.4f} -> {ll_new:.4f} (n={n}); applied {cfg.learn_max_step:.0%} of the step",
                     newp, n, ll_old, ll_new)


def _vol_multiplier(rows, current: float, cfg) -> float:
    """If realised outcomes are less extreme than |z| implies, the model is overconfident -> raise sigma.
    Test: among samples with |z|>1, the model's own q_model should hit at the average q_model rate."""
    zs, qs, ys = [], [], []
    for r in rows:
        f = json.loads(r["features"]) if isinstance(r["features"], str) else (r["features"] or {})
        if "abs_z" in f and f["abs_z"] > 1.0 and r["q_model"] is not None:
            qm = r["q_model"]
            side_prob = qm if r["side"] == "Up" else 1 - qm
            qs.append(side_prob); ys.append(1.0 if r["won"] else 0.0)
    if len(qs) < 30:
        return current
    predicted, realised = float(np.mean(qs)), float(np.mean(ys))
    if realised <= 0.5:
        target = current * 1.5
    else:
        # match confidence: shrink |z| by ratio of logits
        target = current * max(0.5, min(2.0, logit(predicted) / max(1e-3, logit(realised))))
    stepped = current + cfg.learn_max_step * (target - current)
    return float(np.clip(stepped, 0.5, 3.0))


def load_params(store) -> Tuple[ModelParams, int]:
    row = store.latest_params()
    if row is None:
        p = ModelParams()
        v = store.save_params(p.to_dict(), "initial prior (equal blend of model and market)", 0, 0.0)
        return p, v
    return ModelParams.from_dict(json.loads(row["params"])), row["version"]
