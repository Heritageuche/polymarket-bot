"""v2 drawdown regime.

v1's hard floor compared only *realised* P/L against the limit, so money sitting in unresolved
positions was invisible to it. On 2026-09-17 (data/polybot.db) that cost the account 45% on a
25% stop: the halt fired at -26.7% with $37.13 still open, and those positions settled anyway.
All eight trades that day were "Up" on btc/eth/sol/xrp, which is one bet placed eight times.

These tests pin the two properties that fix it:
  * the day cannot be made to exceed the hard floor, because open stake is budgeted BEFORE entry;
  * correlated positions share one exposure cap.
"""
import time

import pytest

from polybot.config import Config
from polybot.risk.daily import DayManager
from polybot.strategy.kelly import size_position


# The real 2026-09-17 session, in the order the orders were placed.
SEPT_17_START_EQUITY = 205.89374
SEPT_17_ENTRIES = [          # (seconds after 05:00:00, asset, interval, stake_usd)
    (8,   "btc", "15m", 18.60),
    (37,  "xrp", "15m", 2.08),
    (37,  "eth", "5m",  15.00),
    (59,  "sol", "15m", 10.45),
    (60,  "sol", "5m",  16.50),
    (425, "btc", "5m",  13.50),
    (537, "eth", "5m",  10.00),
    (692, "xrp", "5m",  6.00),
]
SEPT_17_REALISED_LOSS = 92.13   # every one of them resolved against the position


class _Store:
    def log_day(self, *a, **k): pass


@pytest.fixture
def day(tmp_path, monkeypatch):
    import polybot.risk.daily as daily
    monkeypatch.setattr(daily, "DAY_FILE", tmp_path / "day.json")
    cfg = Config()
    d = DayManager(cfg, _Store())
    d.roll_if_needed(SEPT_17_START_EQUITY)
    return d


def test_v1_actually_blew_through_its_own_floor():
    """Baseline: the loss we are fixing really was ~1.8x the stop it was meant to respect."""
    floor_usd = 0.25 * SEPT_17_START_EQUITY
    assert SEPT_17_REALISED_LOSS == pytest.approx(92.13)
    assert SEPT_17_REALISED_LOSS > 1.7 * floor_usd
    assert SEPT_17_REALISED_LOSS / SEPT_17_START_EQUITY == pytest.approx(0.447, abs=0.002)


def test_budget_bounds_the_worst_case_day(day):
    """Replay 2026-09-17 with the budget enforced: total money at risk can never exceed the floor,
    so even a day where EVERY position loses in full stops at the floor instead of 45%."""
    floor_usd = day.day.hard_floor_frac * SEPT_17_START_EQUITY
    at_risk = 0.0
    admitted = []
    for _t, asset, interval, stake in SEPT_17_ENTRIES:
        budget = day.risk_budget_usd(at_risk)
        allowed = max(0.0, min(stake, budget))
        if allowed <= 0:
            continue
        admitted.append((asset, interval, allowed))
        at_risk += allowed
        assert at_risk <= floor_usd + 1e-9, "open stake must never exceed the day's floor"

    # now settle every single one as a total loss - the worst case the market can produce
    for _asset, _interval, stake in admitted:
        at_risk -= stake
        day.on_trade_resolved(pnl=-stake, ev=0.0, var=stake ** 2)

    loss = -day.day.realized_pnl
    assert loss <= floor_usd + 1e-9, f"worst-case day {loss:.2f} breached floor {floor_usd:.2f}"
    assert loss < SEPT_17_REALISED_LOSS, "v2 must lose strictly less than v1 did on this day"
    assert day.day.loss_halt and not day.entries_allowed


def test_budget_refuses_entries_once_exhausted(day):
    """A day already at its floor has no capacity, whether it was spent or is still on the table."""
    assert day.risk_budget_usd(at_risk_usd=0.0) > 0
    floor_usd = day.day.hard_floor_frac * SEPT_17_START_EQUITY
    assert day.risk_budget_usd(at_risk_usd=floor_usd) == pytest.approx(0.0)
    # realised losses consume the same budget as open stake
    day.on_trade_resolved(pnl=-floor_usd / 2, ev=0.0, var=1.0)
    assert day.risk_budget_usd(at_risk_usd=floor_usd / 2) == pytest.approx(0.0)


def test_mark_to_market_halt_fires_before_anything_resolves(day):
    """The v1 gap: equity falling on open positions triggered nothing until they settled."""
    assert not day.day.loss_halt
    equity = SEPT_17_START_EQUITY * (1 - day.day.hard_floor_frac - 0.01)
    day.update_equity(equity)
    assert day.day.loss_halt, "a drawdown carried by open positions must halt entries"
    assert "mark-to-market" in day.day.halt_reason
    assert day.day.realized_pnl == 0.0, "nothing had resolved - this is the point of the check"


def test_mark_to_market_halt_can_be_disabled(day):
    day.cfg.mark_to_market_halt = False
    day.update_equity(SEPT_17_START_EQUITY * 0.5)
    assert not day.day.loss_halt


def test_correlated_positions_share_one_cap():
    """Four 'Up' positions on four coins that move together are one bet, not four."""
    cfg = Config(max_cluster_exposure=0.15, min_edge=0.01)
    equity = 205.89
    common = dict(q=0.62, price=0.50, fee_per_share=0.0, equity=equity, cfg=cfg,
                  n_eff=200, current_exposure_frac=0.0, min_shares=5)

    fresh = size_position(**common, cluster_exposure_frac=0.0)
    assert fresh.shares > 0

    crowded = size_position(**common, cluster_exposure_frac=0.14)
    assert crowded.fraction <= 0.01 + 1e-9, "a nearly-full cluster must leave almost no room"

    full = size_position(**common, cluster_exposure_frac=0.15)
    assert full.shares == 0 and full.stake_usd == 0
    assert full.reason == "no risk capacity left"


def test_cluster_cap_does_not_punish_an_uncorrelated_book():
    """The cap is per correlated group, so an unrelated position must not shrink this one."""
    cfg = Config(max_cluster_exposure=0.15, min_edge=0.01)
    a = size_position(q=0.62, price=0.50, fee_per_share=0.0, equity=205.89, cfg=cfg, n_eff=200,
                      current_exposure_frac=0.0, min_shares=5, cluster_exposure_frac=0.0)
    b = size_position(q=0.62, price=0.50, fee_per_share=0.0, equity=205.89, cfg=cfg, n_eff=200,
                      current_exposure_frac=0.10, min_shares=5, cluster_exposure_frac=0.0)
    assert a.shares == b.shares


def test_budget_shrinks_the_order_rather_than_only_rejecting_it():
    """Partial capacity should still trade - just smaller."""
    cfg = Config(min_edge=0.01)
    equity = 205.89
    full = size_position(q=0.62, price=0.50, fee_per_share=0.0, equity=equity, cfg=cfg, n_eff=200,
                         current_exposure_frac=0.0, min_shares=5, budget_usd=float("inf"))
    tight = size_position(q=0.62, price=0.50, fee_per_share=0.0, equity=equity, cfg=cfg, n_eff=200,
                          current_exposure_frac=0.0, min_shares=5, budget_usd=6.0)
    assert 0 < tight.stake_usd <= 6.0 < full.stake_usd


def test_cluster_key_groups_by_direction():
    from polybot.engine import Engine
    cfg = Config(cluster_by="side")
    assert Engine.cluster_key(type("E", (), {"cfg": cfg})(), "btc", "Up") == \
           Engine.cluster_key(type("E", (), {"cfg": cfg})(), "eth", "Up")
    cfg2 = Config(cluster_by="asset_side")
    assert Engine.cluster_key(type("E", (), {"cfg": cfg2})(), "btc", "Up") != \
           Engine.cluster_key(type("E", (), {"cfg": cfg2})(), "eth", "Up")


# ---------------------------------------------------------------- engine-level integration

def _engine(tmp_path, monkeypatch, **overrides):
    from test_halt_behaviour import build_engine
    eng, store, m = build_engine(tmp_path, monkeypatch)
    for k, v in overrides.items():
        setattr(eng.cfg, k, v)
    # the paper broker reaches for a live CLOB book when an order is placed; keep the test offline
    monkeypatch.setattr(eng.broker, "book", lambda tok: eng.book(tok))
    return eng, store, m


def test_entry_cooldown_stops_firing_the_book_into_one_signal(tmp_path, monkeypatch):
    """05:00:08 -> 05:01:00 on 2026-09-17: five positions in 52 seconds, all the same direction."""
    eng, store, m = _engine(tmp_path, monkeypatch, min_seconds_between_entries=60)
    eng.day.roll_if_needed(103.65)
    now = time.time()
    eng.last_entry_at = now - 5          # an entry went out 5 seconds ago
    eng.scan_entries(now, live=True)
    rows = store.db.execute("SELECT skip_reason FROM signals").fetchall()
    assert rows and "cooldown" in (rows[0]["skip_reason"] or "")
    assert not eng.broker.positions(), "no second position inside the cooldown window"


def test_entry_allowed_once_the_cooldown_has_passed(tmp_path, monkeypatch):
    eng, store, m = _engine(tmp_path, monkeypatch, min_seconds_between_entries=60)
    eng.day.roll_if_needed(103.65)
    now = time.time()
    eng.last_entry_at = now - 120
    eng.scan_entries(now, live=True)
    assert eng.broker.positions() or eng.pending, "a cooled-down bot must still trade"


def test_engine_reports_an_exhausted_budget_as_a_skip_reason(tmp_path, monkeypatch):
    eng, store, m = _engine(tmp_path, monkeypatch, min_seconds_between_entries=0)
    eng.day.roll_if_needed(103.65)
    eng.day.day.realized_pnl = -eng.day.day.hard_floor_frac * 103.65   # floor already spent
    eng.scan_entries(time.time(), live=True)
    rows = store.db.execute("SELECT skip_reason FROM signals").fetchall()
    assert rows and "budget" in (rows[0]["skip_reason"] or "")
    assert not eng.broker.positions()
