"""Post-trade review: what worked, what failed, which buckets to block, and whether the operator's
'-10% => bail' habit is actually +EV on our own recorded price paths."""
from __future__ import annotations
import json
import math
from collections import defaultdict
from typing import Dict, List, Tuple


def wilson_ucb(mean: float, sd: float, n: int, z: float = 1.64) -> float:
    return mean + z * sd / math.sqrt(max(1, n))


def bucket_keys(r) -> List[str]:
    f = json.loads(r["features"]) if isinstance(r["features"], str) else (r["features"] or {})
    edge = r["edge"] or 0.0
    side = r["outcome_side"] if "outcome_side" in r.keys() else r["side"]
    keys = [f"asset:{r['asset']}", f"interval:{r['interval']}",
            f"edge:{'lo' if edge < 0.05 else 'mid' if edge < 0.10 else 'hi'}"]
    if "abs_z" in f:
        keys.append(f"absz:{'<0.5' if f['abs_z'] < 0.5 else '<1' if f['abs_z'] < 1 else '>=1'}")
    if "tau_eff" in f:
        keys.append(f"tau:{'<2m' if f['tau_eff'] < 120 else '<10m' if f['tau_eff'] < 600 else '>=10m'}")
    if "mom_z" in f:
        keys.append(f"mom:{'with' if (f['mom_z'] > 0) == (side == 'Up') else 'against'}")
    return keys


def bucket_stats(trades) -> Dict[str, dict]:
    acc = defaultdict(list)
    for t in trades:
        roi = (t["pnl"] or 0.0) / t["stake_usd"] if t["stake_usd"] else 0.0
        for k in bucket_keys(t):
            acc[k].append((roi, 1.0 if t["won"] else 0.0, t["q_est"] or 0.5))
    out = {}
    for k, v in acc.items():
        n = len(v)
        rois = [x[0] for x in v]; wins = [x[1] for x in v]; qs = [x[2] for x in v]
        mean = sum(rois) / n; sd = math.sqrt(sum((x - mean) ** 2 for x in rois) / max(1, n - 1)) if n > 1 else 1.0
        out[k] = {"n": n, "roi": mean, "roi_ucb": wilson_ucb(mean, sd, n), "win_rate": sum(wins) / n,
                  "avg_q": sum(qs) / n, "brier": sum((q - w) ** 2 for q, w in zip(qs, wins)) / n}
    return out


def blocked_buckets(stats: Dict[str, dict], min_n: int) -> Dict[str, Tuple[int, float, float]]:
    """Block a bucket when it has enough trades and even its optimistic ROI bound is negative."""
    return {k: (s["n"], s["roi"], s["roi_ucb"]) for k, s in stats.items() if s["n"] >= min_n and s["roi_ucb"] < 0}


def calibration_table(signals, bins: int = 5) -> List[dict]:
    rows = []
    buckets = defaultdict(list)
    for s in signals:
        q = s["q_est"] if s["side"] == "Up" else 1 - (s["q_est"] or 0.5)
        q = s["q_est"] or 0.5
        buckets[min(bins - 1, int(q * bins))].append((q, 1.0 if s["won"] else 0.0))
    for b in sorted(buckets):
        v = buckets[b]
        rows.append({"bucket": f"{b/bins:.1f}-{(b+1)/bins:.1f}", "n": len(v),
                     "predicted": sum(x[0] for x in v) / len(v), "realised": sum(x[1] for x in v) / len(v)})
    return rows


def stop_rule_counterfactual(store, thresholds=(-0.05, -0.10, -0.20, -0.30)) -> Dict[str, dict]:
    """Replay recorded mark-to-market paths: what if we had sold the first time unrealised P/L crossed
    each threshold (at the recorded bid, paying the taker fee), versus what actually happened?
    Reported in log-growth units per trade so it is comparable with Kelly's objective."""
    trades = store.resolved_trades()
    out = {}
    for th in thresholds:
        g_actual = g_rule = 0.0; n = 0; triggered = 0
        for t in trades:
            marks = store.marks_for(t["id"])
            stake = t["stake_usd"] or 0
            if not marks or stake <= 0:
                continue
            equity_ref = max(stake * 5, 50.0)   # growth measured against a reference equity
            pnl_actual = t["pnl"] or 0.0
            pnl_rule = pnl_actual
            for m in marks:
                if m["pnl_pct"] is not None and m["pnl_pct"] <= th and m["bid"]:
                    fee = 0.07 * m["bid"] * (1 - m["bid"])
                    pnl_rule = t["shares"] * (m["bid"] - fee) - stake
                    triggered += 1
                    break
            g_actual += math.log(max(1e-6, 1 + pnl_actual / equity_ref))
            g_rule += math.log(max(1e-6, 1 + pnl_rule / equity_ref))
            n += 1
        out[f"exit at {th:+.0%}"] = {"n": n, "triggered": triggered,
                                    "growth_actual": g_actual / n if n else 0, "growth_rule": g_rule / n if n else 0,
                                    "verdict": ("rule better" if g_rule > g_actual + 1e-6 else "hold better") if n else "no data"}
    return out


def report(store, cfg) -> str:
    trades = store.resolved_trades()
    sigs = store.resolved_signals()
    lines = [f"Resolved trades: {len(trades)}   resolved signals (incl. shadow): {len(sigs)}"]
    if trades:
        pnl = sum(t["pnl"] or 0 for t in trades); stake = sum(t["stake_usd"] or 0 for t in trades)
        wins = sum(1 for t in trades if t["won"])
        lines.append(f"P/L ${pnl:+.2f} on ${stake:.2f} staked (ROI {pnl/stake:+.1%}), win rate {wins/len(trades):.1%}, "
                     f"avg q {sum(t['q_est'] or 0 for t in trades)/len(trades):.3f}")
        mgmt = defaultdict(int)
        for t in trades:
            mgmt[t["management"] or "held"] += 1
        lines.append("management: " + ", ".join(f"{k}={v}" for k, v in mgmt.items()))
        lines.append("\nBuckets (n, ROI, ROI upper bound, win rate, Brier):")
        st = bucket_stats(trades)
        for k in sorted(st):
            s = st[k]
            flag = "  <-- BLOCKED" if s["n"] >= cfg.block_bucket_min_n and s["roi_ucb"] < 0 else ""
            lines.append(f"  {k:14s} n={s['n']:4d} roi={s['roi']:+.3f} ucb={s['roi_ucb']:+.3f} win={s['win_rate']:.2f} brier={s['brier']:.3f}{flag}")
        lines.append("\nStop-rule counterfactual (log growth per trade, actual vs rule):")
        for k, v in stop_rule_counterfactual(store).items():
            lines.append(f"  {k:12s} n={v['n']:4d} triggered={v['triggered']:4d} actual={v['growth_actual']:+.5f} rule={v['growth_rule']:+.5f}  {v['verdict']}")
    if sigs:
        lines.append("\nCalibration (predicted vs realised, all resolved signals):")
        for r in calibration_table(sigs):
            lines.append(f"  q in {r['bucket']}: n={r['n']:4d} predicted={r['predicted']:.3f} realised={r['realised']:.3f}")
    row = store.latest_params()
    if row:
        lines.append(f"\nModel params v{row['version']}: {row['params']}  ({row['note']})")
    b = store.blocked()
    if b:
        lines.append("Blocked buckets: " + ", ".join(sorted(b)))
    return "\n".join(lines)
