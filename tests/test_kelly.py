import math
from polybot.strategy.kelly import (kelly_fraction, drawdown_multiplier, uncertainty_shrink, q_std,
                                    expected_log_growth, size_position)
from polybot.config import Config


def test_kelly_binary_matches_textbook():
    # q=0.6 at price 0.5 -> b=1 -> f = 2q-1 = 0.2
    assert abs(kelly_fraction(0.6, 0.5) - 0.2) < 1e-12
    assert kelly_fraction(0.5, 0.5) == 0.0
    assert kelly_fraction(0.4, 0.5) == 0.0


def test_kelly_maximises_growth():
    q, c = 0.62, 0.55
    f = kelly_fraction(q, c)
    g = expected_log_growth(q, c, f)
    for d in (-0.02, 0.02):
        assert expected_log_growth(q, c, f + d) < g


def test_drawdown_multiplier():
    k = drawdown_multiplier(0.5, 0.05)
    assert 0.37 < k < 0.38
    # check the inversion: P(DD>=x) = x^(2/k - 1)
    assert abs(0.5 ** (2 / k - 1) - 0.05) < 1e-9
    assert drawdown_multiplier(0.5, 0.2) > k   # tolerate more risk -> bigger multiplier


def test_uncertainty_shrink():
    assert uncertainty_shrink(0.08, 0.08) == 0.5
    assert uncertainty_shrink(0.08, 0.0) == 1.0
    assert uncertainty_shrink(-0.01, 0.05) == 0.0
    assert q_std(0.5, 0, 0.05) > q_std(0.5, 400, 0.05) > 0.05


def test_size_position_respects_minimum_and_caps():
    cfg = Config()
    d = size_position(q=0.58, price=0.50, fee_per_share=0.0, equity=103.60, cfg=cfg, n_eff=0,
                      current_exposure_frac=0.0, min_shares=5)
    assert d.shares >= 5 and d.stake_usd <= cfg.max_fraction_per_trade * 103.60
    d2 = size_position(q=0.52, price=0.50, fee_per_share=0.0175, equity=103.60, cfg=cfg, n_eff=0,
                       current_exposure_frac=0.0, min_shares=5)
    assert d2.shares == 0  # edge below min_edge after fee
    d3 = size_position(q=0.9, price=0.50, fee_per_share=0.0, equity=103.60, cfg=cfg, n_eff=1000,
                       current_exposure_frac=0.0, min_shares=5)
    assert d3.fraction <= cfg.max_fraction_per_trade + 1e-9
    d4 = size_position(q=0.9, price=0.50, fee_per_share=0.0, equity=103.60, cfg=cfg, n_eff=1000,
                       current_exposure_frac=cfg.max_total_exposure, min_shares=5)
    assert d4.shares == 0
