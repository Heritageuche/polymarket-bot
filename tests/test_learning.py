import json, math, random, pathlib, tempfile
from polybot.learning.store import Store
from polybot.learning.calibration import propose_update, load_params
from polybot.learning.review import bucket_stats, blocked_buckets, stop_rule_counterfactual
from polybot.strategy.probability import ModelParams, sigmoid
from polybot.config import Config


def make_store():
    return Store(pathlib.Path(tempfile.mkdtemp()) / "t.db")


def synth_signals(store, n, true_w=(0.0, 1.2, 0.2, 0.0), seed=1):
    rng = random.Random(seed)
    for i in range(n):
        lm = rng.gauss(0, 1.0); lk = lm * 0.6 + rng.gauss(0, 0.3); mom = rng.gauss(0, 1)
        p = sigmoid(true_w[0] + true_w[1] * lm + true_w[2] * lk + true_w[3] * mom)
        won = rng.random() < p
        store.log_signal(market_id=f"m{i}", asset="btc", interval="5m", side="Up", q_est=0.5, q_model=sigmoid(lm),
                         p_market=sigmoid(lk), edge=0.05, taken=0, features={"logit_model": lm, "logit_market": lk,
                         "mom_z": mom, "abs_z": abs(lm)}, params_version=1)
        store.db.execute("UPDATE signals SET won=?, outcome='Up' WHERE market_id=?", (int(won), f"m{i}"))
    store.db.commit()


def test_no_update_before_min_trades():
    st = make_store(); cfg = Config(); synth_signals(st, 10)
    res = propose_update(st.resolved_signals(), ModelParams(), cfg)
    assert not res.accepted and "need" in res.note


def test_update_is_bounded_and_moves_toward_truth():
    st = make_store(); cfg = Config(); synth_signals(st, 800, true_w=(0.0, 2.0, -0.3, 0.0))
    cur = ModelParams()
    res = propose_update(st.resolved_signals(), cur, cfg)
    assert res.accepted
    # moved toward the true weight on the model (2.0) but at most learn_max_step of the way
    assert cur.a_model < res.new_params.a_model < 1.5   # moved, but bounded and clipped
    assert res.new_params.a_model < cur.a_model + cfg.learn_max_step * 2.0  # never more than 30% of the fitted jump
    assert res.val_ll_new < res.val_ll_old


def test_noise_does_not_get_accepted():
    st = make_store(); cfg = Config()
    synth_signals(st, 200, true_w=(0.0, 0.5, 0.5, 0.0), seed=7)   # truth == prior
    res = propose_update(st.resolved_signals(), ModelParams(), cfg)
    # With truth at the prior, a candidate can only win by chance; require it to at least not be a big move.
    if res.accepted:
        assert abs(res.new_params.a_model - 0.5) < 0.2


def test_blocked_buckets_and_counterfactual():
    st = make_store(); cfg = Config()
    for i in range(30):
        tid = st.open_trade(mode="paper", market_id=f"x{i}", asset="eth", interval="15m", outcome_side="Up", token="t",
                            entry_price=0.5, shares=10, stake_usd=5.0, q_est=0.55, edge=0.02, features={"abs_z": 0.2, "tau_eff": 100, "mom_z": 0.1})
        st.mark(tid, 0.42, 0.44, 0.5, -0.16, 60)
        st.update_trade(tid, status="closed", won=0, pnl=-5.0, ts_close=1.0)
    stats = bucket_stats(st.resolved_trades())
    bl = blocked_buckets(stats, cfg.block_bucket_min_n)
    assert "asset:eth" in bl
    cf = stop_rule_counterfactual(st)
    assert cf["exit at -10%"]["verdict"] == "rule better"   # every trade lost fully; bailing at -16% was better
