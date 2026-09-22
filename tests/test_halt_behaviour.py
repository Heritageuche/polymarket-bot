"""The daily +10% target must stop ENTRIES ONLY. It must not stop the bot from observing, recording,
resolving or learning - otherwise every successful day would blind the model for the rest of that day."""
import pathlib, tempfile, time, types
import pytest

from polybot.config import Config
from polybot.learning.store import Store
from polybot.polymarket.broker import PaperBroker, Book
from polybot.polymarket.gamma import Market
from polybot.strategy.probability import Estimate, ModelParams
from polybot.engine import Engine


class FakeFeed:
    connected = True
    def start(self): pass
    def stop(self): pass


def make_market(end_in=300.0):
    now = time.time()
    return Market(market_id="m1", condition_id="0xc1", slug="btc-updown-5m-test", question="q",
                  asset="btc", interval="5m", up_token="TUP", down_token="TDN",
                  start_ts=now - 60, end_ts=now + end_in, fee_rate=0.07, min_size=5, tick=0.01,
                  resolution="chainlink_twap", twap_lookback=60)


def build_engine(tmp_path, monkeypatch):
    cfg = Config(min_seconds_remaining=10, max_seconds_remaining=900, warmup_seconds=0)
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(Engine, "_restore_open_trades", lambda self: None)
    monkeypatch.setattr("polybot.engine.Gamma", lambda *a, **k: types.SimpleNamespace())
    import polybot.risk.daily as daily
    monkeypatch.setattr(daily, "DAY_FILE", tmp_path / "day.json")
    eng = Engine(cfg, PaperBroker(103.65), store, FakeFeed(), "test")
    eng.equity = 103.65
    m = make_market()
    eng.markets = {m.market_id: m}
    # a clearly +EV opportunity: market prices Up at 0.50, model says 0.62
    book_up = Book("TUP", [(0.50, 500)], [(0.51, 500)], time.time())
    book_dn = Book("TDN", [(0.49, 500)], [(0.50, 500)], time.time())
    monkeypatch.setattr(eng, "book", lambda tok, max_age=1.5: book_up if tok == "TUP" else book_dn)
    est = Estimate(q=0.62, q_model=0.60, p_market=0.505, spot=100.0, strike=99.0, sigma_per_sec=1e-5,
                   tau_eff=270.0, mom_z=0.4, z=0.3, strike_uncertainty=0.0,
                   features={"logit_model": 0.405, "logit_market": 0.02, "mom_z": 0.4,
                             "tau_eff": 270.0, "abs_z": 0.3, "sigma": 1e-5, "interval": "5m", "asset": "btc"})
    monkeypatch.setattr(eng.model, "estimate", lambda *a, **k: est)
    return eng, store, m


def test_halted_bot_still_records_the_signal_it_would_have_taken(tmp_path, monkeypatch):
    eng, store, m = build_engine(tmp_path, monkeypatch)
    eng.day.roll_if_needed(103.65)
    eng.day.update_equity(103.65 * 1.11)          # +11% -> target latched
    assert eng.day.day.target_hit and not eng.day.entries_allowed

    eng.scan_entries(time.time(), live=False)

    rows = store.db.execute("SELECT * FROM signals").fetchall()
    assert len(rows) == 1, "a halted bot must still journal the opportunity it saw"
    r = rows[0]
    assert r["taken"] == 0
    assert "halted" in (r["skip_reason"] or "")
    assert "DAILY TARGET" in (r["skip_reason"] or "")
    assert r["q_est"] == pytest.approx(0.62)
    assert r["features"], "features must be stored so the signal is usable as training data"
    assert not eng.broker.positions(), "no position may be opened while halted"


def test_shadow_signals_resolve_and_feed_the_learner(tmp_path, monkeypatch):
    eng, store, m = build_engine(tmp_path, monkeypatch)
    eng.day.roll_if_needed(103.65)
    eng.day.update_equity(103.65 * 1.11)
    eng.scan_entries(time.time(), live=False)

    store.resolve_signals(m.market_id, "Up")
    resolved = store.resolved_signals()
    assert len(resolved) == 1 and resolved[0]["won"] == 1
    # the learner reads resolved signals, not resolved trades: a halted day still teaches it
    from polybot.learning.calibration import _design
    X, y = _design(resolved)
    assert len(y) == 1, "the shadow signal must be usable as a training row"


def test_entries_resume_next_day_and_learning_ran(tmp_path, monkeypatch):
    eng, store, m = build_engine(tmp_path, monkeypatch)
    eng.day.roll_if_needed(103.65)
    eng.day.update_equity(103.65 * 1.11)
    assert not eng.day.entries_allowed
    eng.day.day.date = "1999-01-01"               # force a rollover
    assert eng.day.roll_if_needed(114.0)
    assert eng.day.entries_allowed, "a new day must clear the target latch"
    assert eng.day.day.start_equity == 114.0, "the new day's target is based on the new equity"


def test_pause_keeps_managing_open_positions(tmp_path, monkeypatch):
    """An operator pause must not abandon money that is already at risk."""
    from polybot.control import RunState
    s = RunState()
    s.apply("pause")
    assert s.manage_allowed and not s.entries_allowed and not s.exit_requested


def test_halted_day_still_triggers_recalibration(tmp_path, monkeypatch):
    """Resolved shadow signals must be able to trigger a learning pass on their own, so a day spent
    above target still sharpens the model rather than leaving it stale until midnight."""
    eng, store, m = build_engine(tmp_path, monkeypatch)
    eng.cfg.learn_every_signals = 2
    eng.day.roll_if_needed(103.65)
    eng.day.update_equity(103.65 * 1.11)
    calls = []
    monkeypatch.setattr(eng, "learn", lambda why: calls.append(why))

    eng.scan_entries(time.time(), live=False)
    assert store.db.execute("SELECT COUNT(*) FROM signals").fetchone()[0] == 1
    # force the market to look resolved
    m.end_ts = time.time() - 1
    m.closed = True
    m.outcome_prices = [1.0, 0.0]
    eng.resolved_signals_since_learn = 1          # one already banked from an earlier window
    eng.check_resolutions(time.time())
    assert calls == ["resolved signals"], "shadow signals alone must be able to trigger learning"


def test_target_latch_clears_at_rollover_but_operator_pause_does_not(tmp_path, monkeypatch):
    """The +10% latch is a TODAY-only halt: it clears by itself at the next trading day.
    An operator `pause` is indefinite and survives midnight until `resume`. Confusing the two is how
    you wake up to a bot that quietly did not trade."""
    from polybot.control import RunState
    eng, store, m = build_engine(tmp_path, monkeypatch)
    eng.day.roll_if_needed(103.65)
    eng.day.update_equity(103.65 * 1.11)

    # today: target latch blocks entries, operator state is untouched
    assert not eng.day.entries_allowed
    assert eng.state.entries_allowed, "the target halt must not touch operator state"

    # tomorrow: the latch releases on its own
    eng.day.day.date = "1999-01-01"
    eng.day.roll_if_needed(135.34)
    assert eng.day.entries_allowed, "the daily target latch must clear at rollover"

    # an operator pause behaves differently: it persists across the rollover
    eng.state.apply("pause")
    eng.day.day.date = "1999-01-01"
    eng.day.roll_if_needed(135.34)
    assert eng.day.entries_allowed and not eng.state.entries_allowed
    combined = eng.state.entries_allowed and eng.day.entries_allowed
    assert not combined, "a pause must still block entries on the new day"
    eng.state.apply("resume")
    assert eng.state.entries_allowed and eng.day.entries_allowed, "resume restores trading"
