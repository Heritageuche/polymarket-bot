"""Regression tests for the feed bugs found while the bot was live and flat:
a reconnect that inherited the previous connection's stall timer, and a stale price being priced as live."""
import time
import pytest
from polybot.market_data.feed import Series, MAX_PRICE_AGE, STALL_SECONDS, MIN_RECONNECT_GAP
from polybot.strategy.probability import ProbabilityModel, ModelParams
from polybot.polymarket.gamma import Market


class StubFeed:
    def __init__(self, series): self._s = series
    def series(self, resolution, cl, bn): return self._s
    def vol_per_sec(self, s, bn=None): return 2e-5
    def strike(self, s, start_ts, bn): return 100.0, 0.0


def make_series(last_age_s, now):
    s = Series()
    for i in range(120, 0, -1):
        s.add(now - last_age_s - i, 100.0 + (i % 3) * 0.01)
    return s


def make_market(now):
    return Market(market_id="m", condition_id="c", slug="s", question="q", asset="btc", interval="5m",
                  up_token="U", down_token="D", start_ts=now - 60, end_ts=now + 240, fee_rate=0.07,
                  min_size=5, tick=0.01, resolution="chainlink_twap", twap_lookback=60)


def test_stale_prices_are_refused():
    now = time.time()
    fresh = ProbabilityModel(StubFeed(make_series(1.0, now)), ModelParams())
    assert fresh.estimate(make_market(now), 0.5, now) is not None, "fresh data must price normally"
    stale = ProbabilityModel(StubFeed(make_series(MAX_PRICE_AGE + 10, now)), ModelParams())
    assert stale.estimate(make_market(now), 0.5, now) is None, "stale data must not produce a tradable estimate"


def test_market_not_yet_open_is_refused():
    now = time.time()
    m = make_market(now)
    m.start_ts = now + 30          # window has not begun, so no strike exists yet
    model = ProbabilityModel(StubFeed(make_series(1.0, now)), ModelParams())
    assert model.estimate(m, 0.5, now) is None


def test_stall_budget_is_generous_enough_for_a_quiet_oracle():
    # the live feed was observed delivering ~1 message every 9s during a quiet spell;
    # the stall threshold must not mistake that for a dead socket
    assert STALL_SECONDS >= 45
    assert MIN_RECONNECT_GAP >= 1.0


def test_reconnect_resets_the_stall_timer():
    """The bug: last_msg survived a reconnect, so each new socket was killed within one loop pass.
    Asserted on the source because the behaviour lives inside the async reconnect loop."""
    import inspect, polybot.market_data.feed as F
    src = inspect.getsource(F.Feed._loop)
    subscribe = src.index('"action": "subscribe"')
    reset = src.index("self.last_msg = time.time()")
    stall = src.index("self.last_msg > STALL_SECONDS")
    assert subscribe < reset < stall, "the stall timer must be reset after subscribing, before it is checked"
