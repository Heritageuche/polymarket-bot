"""CLI.  python -m polybot <command>

  run [--live|--paper] [--config PATH]   start the engine (paper is the default)
  stop [--soft|--flatten|--now]          tell a running engine to stop (soft is the default)
  pause | resume                         halt / allow NEW entries only
  kill [--off]                           create (or remove) control/KILL - bot refuses to start while present
  status                                 show the running engine's status file
  report                                 learning / performance review from the journal
  learn                                  run one controlled learning pass now
  risk [--bankroll X]                    print the Kelly risk framework for a bankroll
  check                                  read-only live-account check: creds, USDC balance, positions, fee rate
"""
from __future__ import annotations
import argparse
import json
import logging
import sys
import time
from .config import load_config
from . import control
from .learning.store import Store


def cmd_run(a):
    cfg = load_config(a.config)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    pid = control.running_pid()
    if pid:
        sys.exit(f"an engine is already running (pid {pid}). Use `polybot stop` first.")
    from .polymarket.gamma import CHAINLINK_SYMBOL, BINANCE_SYMBOL
    from .market_data.feed import Feed
    from .polymarket.broker import PaperBroker, LiveBroker
    from .engine import Engine
    store = Store()
    feed = Feed([CHAINLINK_SYMBOL[x] for x in cfg.assets if x in CHAINLINK_SYMBOL],
                [BINANCE_SYMBOL[x] for x in cfg.assets if x in BINANCE_SYMBOL])
    if a.live:
        if not cfg.has_credentials:
            sys.exit("live mode needs POLY_PRIVATE_KEY and POLY_FUNDER in .env")
        broker = LiveBroker(cfg.private_key, cfg.funder, cfg.signature_type)
        print(f"LIVE mode. USDC balance: ${broker.cash():.2f}")
        mode = "live"
    else:
        broker = PaperBroker(cfg.bankroll_start); mode = "paper"
    control.clear_command()
    Engine(cfg, broker, store, feed, mode).run()


def cmd_stop(a):
    cmd = "stop-flatten" if a.flatten else "stop-now" if a.now else "stop"
    control.write_command(cmd)
    print(f"sent '{cmd}'.", "" if control.running_pid() else "(no engine appears to be running - it will apply on next start)")


def cmd_pause(a):
    control.write_command("pause"); print("sent 'pause' (no new entries; positions still managed)")


def cmd_resume(a):
    control.write_command("resume"); print("sent 'resume'")


def cmd_kill(a):
    if a.off:
        control.set_kill(False); print("KILL file removed")
    else:
        control.set_kill(True); control.write_command("stop-now"); print("KILL file created and stop-now sent")


def cmd_status(a):
    s = control.read_status()
    if not s:
        print("no status file yet"); return
    from . import clock
    age = clock.now() - s.get("heartbeat", 0)
    s["heartbeat_age_s"] = round(age, 1)
    s["engine_alive"] = bool(control.running_pid()) and age < 30
    print(json.dumps(s, indent=1))


def cmd_report(a):
    from .learning.review import report
    print(report(Store(), load_config(a.config)))


def cmd_learn(a):
    from .learning.calibration import propose_update, load_params
    cfg = load_config(a.config); store = Store()
    params, v = load_params(store)
    res = propose_update(store.resolved_signals(), params, cfg)
    print(res.note)
    if res.accepted and res.new_params:
        nv = store.save_params(res.new_params.to_dict(), res.note, res.n, res.val_ll_new)
        print(f"saved as v{nv}: {res.new_params.to_dict()}  (a running engine picks it up on restart)")


def cmd_check(a):
    """Read-only: derives API creds, reads balance and positions, fetches a fee rate. Places no orders."""
    cfg = load_config(a.config)
    if not cfg.has_credentials:
        sys.exit("no credentials: create .env with POLY_PRIVATE_KEY and POLY_FUNDER (see .env.example)")
    from .polymarket.broker import LiveBroker
    from .polymarket.gamma import Gamma
    from .polymarket import wallets
    from eth_account import Account
    import requests as _rq

    signer = Account.from_key(cfg.private_key if cfg.private_key.startswith("0x") else "0x" + cfg.private_key).address
    print(f"signer (from key): {signer}")
    print(f"funder (.env)    : {cfg.funder}")
    print(f"signature_type   : {cfg.signature_type}")
    detected, label = wallets.identify(signer, cfg.funder)
    if detected is None:
        print(f"  WARNING: {label}")
        print("  addresses this key WOULD control:")
        for st, addr in wallets.candidates(signer).items():
            print(f"    signature_type {st}: {addr}")
        print("  -> open polymarket.com, copy the address in your profile menu, and compare it with these.")
    else:
        print(f"  funder matches: {label} (signature_type {detected})")
        if detected != cfg.signature_type:
            print(f"  WARNING: set POLY_SIGNATURE_TYPE={detected} in .env (currently {cfg.signature_type})")
    try:
        v = _rq.get(f"https://data-api.polymarket.com/value?user={cfg.funder}", timeout=10).json()
        print(f"  polymarket portfolio value for funder: ${float(v[0]['value']):.2f}" if v else "  no value record")
    except Exception as e:
        print(f"  value lookup failed: {e}")
    try:
        b = LiveBroker(cfg.private_key, cfg.funder, cfg.signature_type)
    except Exception as e:
        sys.exit(f"could not create/derive API credentials: {e}")
    print("API credentials: ok")
    try:
        cash = b.cash(); print(f"USDC balance (CLOB collateral): ${cash:.2f}")
    except Exception as e:
        print(f"balance lookup FAILED: {e}")
        cash = 0.0
    try:
        pos = b.positions(); print(f"open positions on data-api: {len(pos)}")
        for p in list(pos.values())[:10]:
            print(f"  {p.outcome:5s} {p.shares:8.2f} sh @ {p.avg_price:.3f}  cond {p.condition_id[:10]}..")
    except Exception as e:
        print(f"positions lookup FAILED: {e}")
    try:
        m = Gamma().live_markets(["btc"], ["5m"])[0]
        print(f"fee rate for {m.slug}: {b.client.get_fee_rate_bps(m.up_token)} bps   tick {m.tick} min size {m.min_size}")
    except Exception as e:
        print(f"market/fee lookup FAILED: {e}")
    if cash < 5:
        print("WARNING: balance under $5 - the exchange minimum (5 shares) will block most entries")
    print("check complete - no orders were placed")


def cmd_risk(a):
    from .strategy.kelly import framework_report
    cfg = load_config(a.config)
    print(framework_report(cfg, a.bankroll or cfg.bankroll_start, n_eff=a.n))


def main(argv=None):
    p = argparse.ArgumentParser(prog="polybot", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--live", action="store_true"); r.add_argument("--paper", action="store_true")
    r.add_argument("-v", "--verbose", action="store_true"); r.set_defaults(fn=cmd_run)
    s = sub.add_parser("stop"); s.add_argument("--soft", action="store_true"); s.add_argument("--flatten", action="store_true")
    s.add_argument("--now", action="store_true"); s.set_defaults(fn=cmd_stop)
    sub.add_parser("pause").set_defaults(fn=cmd_pause)
    sub.add_parser("resume").set_defaults(fn=cmd_resume)
    k = sub.add_parser("kill"); k.add_argument("--off", action="store_true"); k.set_defaults(fn=cmd_kill)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("report").set_defaults(fn=cmd_report)
    sub.add_parser("learn").set_defaults(fn=cmd_learn)
    rk = sub.add_parser("risk"); rk.add_argument("--bankroll", type=float); rk.add_argument("--n", type=int, default=0)
    rk.set_defaults(fn=cmd_risk)
    sub.add_parser("check").set_defaults(fn=cmd_check)
    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
