from polybot.strategy.exit_policy import decide, growth


def test_green_position_with_high_q_is_held():
    d = decide(q=0.92, cash=100, shares=20, sell_net=0.85, hedge_net_cost=0.16, min_shares=5, min_improvement=0.0005)
    assert d.action == "hold"


def test_losing_position_held_when_bid_below_q():
    # red on paper (entered at 0.55, bid now 0.40) but our q says 0.50: selling gives away value
    d = decide(q=0.50, cash=100, shares=20, sell_net=0.40 - 0.0168, hedge_net_cost=0.62, min_shares=5, min_improvement=0.0005)
    assert d.action == "hold"


def test_losing_position_sold_when_bid_above_q():
    # price dropped, momentum against us, our q collapsed to 0.25 but bid is still 0.38 -> sell
    d = decide(q=0.25, cash=100, shares=20, sell_net=0.38 - 0.0165, hedge_net_cost=0.65, min_shares=5, min_improvement=0.0005)
    assert d.action in ("sell", "hedge") and d.fraction == 1.0


def test_oversized_position_is_trimmed_not_dumped():
    # position is 80% of wealth; q slightly above net bid -> log utility trims some but not all
    d = decide(q=0.62, cash=25, shares=200, sell_net=0.60, hedge_net_cost=0.45, min_shares=5, min_improvement=0.0005)
    assert d.action == "sell" and 0 < d.fraction < 1


def test_hedge_used_when_cheaper_than_selling():
    d = decide(q=0.20, cash=100, shares=20, sell_net=0.30, hedge_net_cost=0.60, min_shares=5, min_improvement=0.0005)
    assert d.action == "hedge"   # 1-0.60 = 0.40 equivalent > 0.30 sale


def test_hysteresis_blocks_tiny_improvements():
    d = decide(q=0.499, cash=100, shares=10, sell_net=0.50, hedge_net_cost=None, min_shares=5, min_improvement=0.01)
    assert d.action == "hold"
