"""Main loop. One pass every `poll_seconds`:
   control -> equity/day rollover -> discovery -> resolutions -> pending orders -> position management
   -> (entries | shadow signals) -> status.
Stopping/pausing/target-hit only ever touch RunState.entries_allowed / DayManager.entries_allowed."""
from __future__ import annotations
import logging
import math
import time
import traceback
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from .config import Config
from .control import RunState, SignalHandler, read_command, clear_command, write_status, write_pid, kill_file_present
from .polymarket.gamma import Gamma, Market, CHAINLINK_SYMBOL, BINANCE_SYMBOL
from .polymarket.broker import Broker, Order, Book
from .market_data.feed import Feed, binance_candle
from .strategy.probability import ProbabilityModel, Estimate
from .strategy.kelly import size_position, q_std
from .strategy.exit_policy import decide
from .risk.daily import DayManager
from .learning.store import Store
from .learning.calibration import load_params, propose_update
from .learning.review import bucket_stats, blocked_buckets, bucket_keys
from . import clock

log = logging.getLogger("polybot")


@dataclass
class TradeCtx:
    trade_id: int
    market: Market
    side: str
    token: str
    shares: float
    cost_basis: float          # total USDC paid incl. fees
    cash_in: float = 0.0       # USDC received from partial/complete exits
    hedge_shares: float = 0.0  # opposite-side shares bought as a hedge
    hedge_cost: float = 0.0
    entry_price: float = 0.0
    q_entry: float = 0.5
    opened_at: float = 0.0
    ev: float = 0.0
    var: float = 0.0
    min_mark: float = 1.0
    max_mark: float = 0.0
    management: str = "held"
    last_mark: float = 0.0
    last_exit_check: float = 0.0
    reason_close: str = ""


@dataclass
class Pending:
    order: Order
    market: Market
    side: str
    purpose: str               # 'entry' | 'exit' | 'hedge'
    est: Optional[Estimate]
    sizing: object
    ttl: float
    trade: Optional[TradeCtx] = None
    signal_id: Optional[int] = None


class Engine:
    def __init__(self, cfg: Config, broker: Broker, store: Store, feed: Feed, mode: str):
        self.cfg, self.broker, self.store, self.feed, self.mode = cfg, broker, store, feed, mode
        self.state = RunState()
        SignalHandler(self.state)
        self.day = DayManager(cfg, store)
        self.params, self.params_version = load_params(store)
        self.model = ProbabilityModel(feed, self.params)
        self.gamma = Gamma()
        self.markets: Dict[str, Market] = {}
        self.pending: Dict[str, Pending] = {}
        self.trades: Dict[int, TradeCtx] = {}
        self.books: Dict[str, Book] = {}
        self.blocked = store.blocked()
        self.last_discovery = 0.0
        self.resolved_since_learn = 0
        self.resolved_signals_since_learn = 0
        self.resolution_wait: Dict[str, float] = {}
        self.equity = 0.0
        self.started_at = clock.now()
        self._restore_open_trades()

    # ------------------------------------------------------------------ helpers
    def book(self, token: str, max_age: float = 1.5) -> Optional[Book]:
        b = self.books.get(token)
        if b and clock.now() - b.ts < max_age:
            return b
        try:
            b = self.broker.book(token)
            self.books[token] = b
            return b
        except Exception as e:
            log.debug("book fetch failed %s: %s", token[:8], e)
            return b

    def _restore_open_trades(self):
        """After a restart, re-attach to positions the journal says are still open."""
        for r in self.store.open_trades():
            mk = None
            try:
                m = self.gamma._get(f"/markets/{r['market_id']}")
                from .polymarket.gamma import parse_market
                mk = parse_market(m, r["asset"], r["interval"])
            except Exception:
                pass
            if mk is None:
                self.store.update_trade(r["id"], status="orphan", reason_close="market not found on restart")
                continue
            ctx = TradeCtx(r["id"], mk, r["outcome_side"], r["token"], r["shares"], r["stake_usd"],
                           entry_price=r["entry_price"], q_entry=r["q_est"], ev=r["ev_usd"] or 0, var=r["var_usd"] or 0,
                           management=r["management"] or "held")
            self.trades[r["id"]] = ctx
            self.markets[mk.market_id] = mk
        if self.trades:
            log.info("restored %d open trades from journal", len(self.trades))

    def compute_equity(self) -> float:
        try:
            cash = self.broker.cash()
        except Exception as e:
            log.warning("cash lookup failed: %s", e)
            return self.equity or self.cfg.bankroll_start
        val = 0.0
        for ctx in self.trades.values():
            b = self.book(ctx.token, max_age=10)
            px = (b.mid if b and b.mid is not None else ctx.entry_price)
            val += ctx.shares * px
            if ctx.hedge_shares:
                ob = self.book(ctx.market.token_for("Down" if ctx.side == "Up" else "Up"), max_age=10)
                val += ctx.hedge_shares * (ob.mid if ob and ob.mid is not None else 1 - px)
        self.equity = cash + val
        return self.equity

    def exposure_fraction(self) -> float:
        if self.equity <= 0:
            return 1.0
        return sum(c.cost_basis - c.cash_in for c in self.trades.values()) / self.equity

    # ------------------------------------------------------------------ main loop
    def run(self):
        if kill_file_present():
            raise SystemExit("control/KILL present - refusing to start. Run `polybot kill --off` to clear it.")
        write_pid()
        clock.sync(force=True)
        self.started_at = clock.now()
        log.info("clock offset vs exchange time: %+.1fs", clock.offset())
        self.feed.start()
        self.store.event("start", f"mode={self.mode} params_v{self.params_version}")
        log.info("engine started (%s). waiting for price feed...", self.mode)
        t0 = clock.now()
        while not self.feed.connected and clock.now() - t0 < 20:
            time.sleep(0.5)
        try:
            while True:
                loop_start = clock.now()
                try:
                    self.tick()
                except SystemExit:
                    raise
                except Exception:
                    log.error("tick error:\n%s", traceback.format_exc())
                if self.state.exit_now:
                    self.broker.cancel_all(); break
                if self.state.exit_requested and not self.trades and not self.pending:
                    log.info("all positions closed - exiting (%s)", self.state.reason); break
                time.sleep(max(0.2, self.cfg.poll_seconds - (clock.now() - loop_start)))
        finally:
            self.feed.stop()
            self.store.event("stop", self.state.reason)
            write_status(running=False, reason=self.state.reason, mode=self.mode)
            log.info("engine stopped: %s", self.state.reason or "normal")

    def tick(self):
        now = clock.now()
        cmd = read_command()
        if cmd:
            self.state.apply(cmd); clear_command()
            self.store.event("control", cmd); log.info("control command: %s -> %s", cmd, self.state)
        if self.state.exit_now:
            return
        equity = self.compute_equity()
        if self.day.roll_if_needed(equity):
            log.info("new trading day %s, start equity $%.2f", self.day.day.date, equity)
            self.learn("day rollover")
        self.day.update_equity(equity)
        if now - self.last_discovery > self.cfg.discovery_seconds:
            self.discover(); self.last_discovery = now
        self.check_resolutions(now)
        self.manage_pending(now)
        if self.state.manage_allowed:
            self.manage_positions(now)
        if self.state.flatten:
            self.flatten()
        can_enter = self.state.entries_allowed and self.day.entries_allowed and not self.state.exit_requested
        if now - self.started_at < self.cfg.warmup_seconds:
            can_enter = False; self.state.reason = self.state.reason or "warming up"
        elif self.state.reason == "warming up":
            self.state.reason = ""
        self.scan_entries(now, live=can_enter)
        self.write_status(can_enter)

    # ------------------------------------------------------------------ discovery
    def discover(self):
        try:
            found = self.gamma.live_markets(self.cfg.assets, self.cfg.intervals)
        except Exception as e:
            log.warning("discovery failed: %s", e); return
        for m in found:
            old = self.markets.get(m.market_id)
            if old is None:
                self.markets[m.market_id] = m
            else:
                old.closed, old.accepting_orders, old.outcome_prices = m.closed, m.accepting_orders, m.outcome_prices
        # forget markets long resolved and without trades
        for mid in list(self.markets):
            m = self.markets[mid]
            if m.end_ts < clock.now() - 1800 and not any(c.market.market_id == mid for c in self.trades.values()):
                del self.markets[mid]

    # ------------------------------------------------------------------ resolution
    def provisional_outcome(self, m: Market) -> Optional[str]:
        cl, bn = CHAINLINK_SYMBOL.get(m.asset), BINANCE_SYMBOL.get(m.asset)
        if m.resolution == "binance_candle":
            iv = {"1h": "1h", "4h": "4h", "1d": "1d", "15m": "15m", "5m": "5m"}[m.interval]
            c = binance_candle(bn, iv, m.start_ts)
            if c:
                return "Up" if c[1] >= c[0] else "Down"
            return None
        s = self.feed.series(m.resolution, cl, bn)
        k, unc = self.feed.strike(s, m.start_ts, bn)
        with self.feed._lock:
            tw = s.twap(m.end_ts - m.twap_lookback, m.end_ts) if m.twap_lookback else s.at(m.end_ts)
        if k is None or tw is None:
            return None
        if unc > 0 and abs(math.log(tw / k)) < 3 * unc:
            return None       # too close to call with an approximate strike
        return "Up" if tw >= k else "Down"

    def check_resolutions(self, now: float):
        for mid, m in list(self.markets.items()):
            if m.end_ts > now + 1:
                continue
            outcome = m.resolved_outcome
            if outcome is None and now - m.end_ts > 20 and now - self.resolution_wait.get(mid, 0) > 15:
                self.resolution_wait[mid] = now
                try:
                    self.markets[mid] = m = self.gamma.refresh(m)
                except Exception:
                    pass
                outcome = m.resolved_outcome
                if outcome is None and now - m.end_ts > 150:
                    outcome = self.provisional_outcome(m)
                    if outcome:
                        log.info("%s: using provisional outcome %s from price feed", m.slug, outcome)
            if outcome is None:
                continue
            self.resolved_signals_since_learn += self.store.resolve_signals(mid, outcome)
            self.store.close_flat_trades(mid, outcome)   # early-exited trades learn the final outcome too
            for tid, ctx in list(self.trades.items()):
                if ctx.market.market_id == mid:
                    self.settle_trade(ctx, outcome)
            for oid, p in list(self.pending.items()):
                if p.market.market_id == mid:
                    self.broker.cancel(p.order); del self.pending[oid]
            self.markets.pop(mid, None)
            # Shadow signals accumulate even while entries are halted (daily target, pause, loss halt),
            # so the model keeps recalibrating on a halted day instead of going stale until midnight.
            if self.resolved_signals_since_learn >= self.cfg.learn_every_signals:
                self.learn("resolved signals")

    def settle_trade(self, ctx: TradeCtx, outcome: str):
        won = ctx.side == outcome
        credited = self.broker.settle(ctx.market.condition_id, outcome)
        payout = ctx.shares * (1.0 if won else 0.0) + ctx.hedge_shares * (0.0 if won else 1.0)
        pnl = payout + ctx.cash_in - ctx.cost_basis - ctx.hedge_cost
        self.store.update_trade(ctx.trade_id, ts_close=clock.now(), status="closed", outcome=outcome, won=int(won),
                               pnl=pnl, resolution_price=1.0 if won else 0.0, min_mark=ctx.min_mark, max_mark=ctx.max_mark,
                               max_drawdown_pct=(ctx.min_mark - ctx.entry_price) / ctx.entry_price if ctx.entry_price else 0,
                               management=ctx.management,
                               reason_close=ctx.reason_close or f"held to resolution ({outcome})")
        self.day.on_trade_resolved(pnl, ctx.ev, ctx.var)
        self.store.event("resolved", f"{ctx.market.slug} {ctx.side} won={won} pnl={pnl:+.2f}")
        log.info("RESOLVED %s %s -> %s  P/L %+.2f  (day %+.2f, %d trades) %s", ctx.market.slug, ctx.side, outcome,
                 pnl, self.day.day.realized_pnl, self.day.day.trades, self.day.status)
        del self.trades[ctx.trade_id]
        self.resolved_since_learn += 1
        if self.resolved_since_learn >= self.cfg.learn_every_trades:
            self.learn("periodic")

    # ------------------------------------------------------------------ orders
    def manage_pending(self, now: float):
        for oid, p in list(self.pending.items()):
            o = self.broker.refresh_order(p.order)
            if o.filled > 0 and p.purpose == "entry" and p.trade is None:
                self.open_trade(p, o)
            elif o.filled > 0 and p.purpose in ("exit", "hedge") and p.trade is not None:
                self.apply_fill(p, o)
            done = o.status in ("filled", "cancelled")
            if not done and now - o.created > p.ttl:
                self.broker.cancel(o); o = self.broker.refresh_order(o)
                if o.filled > 0 and p.purpose == "entry" and p.trade is None:
                    self.open_trade(p, o)
                done = True
                if p.purpose == "exit" and p.trade is not None and o.filled < o.size - 1e-9:
                    # maker exit didn't fill: fall back to taking liquidity for the remainder
                    rem = o.size - o.filled
                    mo = self.broker.market_order(p.trade.token, "SELL", rem, p.market.fee_rate,
                                                  p.market.condition_id, p.side)
                    if mo.filled > 0:
                        self.apply_fill(Pending(mo, p.market, p.side, "exit", None, None, 0, p.trade), mo)
            if done:
                del self.pending[oid]

    def open_trade(self, p: Pending, o: Order):
        m, est, sz = p.market, p.est, p.sizing
        shares = o.filled
        cost = shares * o.avg_fill + o.fee_paid
        q_side = est.q if p.side == "Up" else 1 - est.q
        ev = shares * q_side - cost
        var = shares * shares * q_side * (1 - q_side)
        tid = self.store.open_trade(mode=self.mode, market_id=m.market_id, condition_id=m.condition_id, slug=m.slug,
                                    asset=m.asset, interval=m.interval, outcome_side=p.side, token=o.token,
                                    entry_price=o.avg_fill, entry_fee=o.fee_paid, shares=shares, stake_usd=cost,
                                    fraction=cost / max(1e-9, self.equity), kelly_full=sz.f_full, kelly_shrunk=sz.f_shrunk,
                                    kelly_mult=sz.k_dd, q_est=q_side, q_model=est.q_model, p_market=est.p_market,
                                    edge=sz.edge, ev_usd=ev, var_usd=var, sigma=est.sigma_per_sec, tau_entry=est.tau_eff,
                                    mom_z=est.mom_z, abs_z=abs(est.z), strike=est.strike, spot_entry=est.spot,
                                    signal_strength=sz.edge / q_std(q_side, self.params.n_fit, self.cfg.model_uncertainty_floor),
                                    maker=int(o.maker), management="held", reason_open=p.reason if hasattr(p, "reason") else "",
                                    params_version=self.params_version, features=dict(est.features, side=p.side))
        ctx = TradeCtx(tid, m, p.side, o.token, shares, cost, entry_price=o.avg_fill, q_entry=q_side, ev=ev, var=var,
                       min_mark=o.avg_fill, max_mark=o.avg_fill, opened_at=clock.now())
        self.trades[tid] = ctx; p.trade = ctx
        if p.signal_id:
            self.store.db.execute("UPDATE signals SET taken=1 WHERE id=?", (p.signal_id,)); self.store.db.commit()
        log.info("OPENED %s %s: %.0f sh @ %.3f (%s) q=%.3f edge=%+.3f f=%.3f $%.2f", m.slug, p.side, shares,
                 o.avg_fill, "maker" if o.maker else "taker", q_side, sz.edge, sz.fraction, cost)

    def apply_fill(self, p: Pending, o: Order):
        ctx = p.trade
        new = o.filled - getattr(o, "_applied", 0.0)
        if new <= 0:
            return
        o._applied = o.filled
        if p.purpose == "exit":
            ctx.shares -= new
            ctx.cash_in += new * o.avg_fill - (o.fee_paid if o.filled == new else 0)
            ctx.management = "exited_early" if ctx.shares <= 1e-9 else "partial_exit"
            log.info("EXIT %s %s: sold %.0f @ %.3f (remaining %.0f)", ctx.market.slug, ctx.side, new, o.avg_fill, ctx.shares)
            self.store.update_trade(ctx.trade_id, exit_price=o.avg_fill, management=ctx.management,
                                    shares=ctx.shares + ctx.hedge_shares if ctx.shares <= 1e-9 else ctx.shares)
            if ctx.shares <= 1e-9 and ctx.hedge_shares <= 1e-9:
                pnl = ctx.cash_in - ctx.cost_basis
                self.store.update_trade(ctx.trade_id, ts_close=clock.now(), status="closed", pnl=pnl, won=None,
                                        outcome="exited", reason_close=ctx.reason_close, shares=ctx.shares)
                # flat, but keep it 'open_flat' until the market resolves so the review can see whether
                # holding would have won (close_flat_trades fills outcome/won at resolution)
                self.store.update_trade(ctx.trade_id, status="open_flat", min_mark=ctx.min_mark, max_mark=ctx.max_mark)
                del self.trades[ctx.trade_id]
                self.day.on_trade_resolved(pnl, ctx.ev, ctx.var)
        else:
            ctx.hedge_shares += new; ctx.hedge_cost += new * o.avg_fill + o.fee_paid
            ctx.management = "hedged"
            log.info("HEDGE %s %s: bought %.0f opposite @ %.3f", ctx.market.slug, ctx.side, new, o.avg_fill)
            self.store.update_trade(ctx.trade_id, management="hedged")

    # ------------------------------------------------------------------ positions
    def manage_positions(self, now: float):
        for tid, ctx in list(self.trades.items()):
            m = ctx.market
            if m.end_ts <= now or any(p.trade is ctx for p in self.pending.values()):
                continue
            if now - ctx.last_exit_check < self.cfg.exit_check_seconds:
                continue
            if ctx.opened_at and now - ctx.opened_at < self.cfg.exit_min_hold_seconds:
                continue
            ctx.last_exit_check = now
            b = self.book(ctx.token)
            if b is None or b.best_bid is None:
                continue
            opp_token = m.token_for("Down" if ctx.side == "Up" else "Up")
            ob = self.book(opp_token)
            p_up = b.mid if ctx.side == "Up" else (1 - b.mid)
            est = self.model.estimate(m, p_up, now)
            q_side = (est.q if ctx.side == "Up" else 1 - est.q) if est else p_up
            ctx.min_mark = min(ctx.min_mark, b.best_bid); ctx.max_mark = max(ctx.max_mark, b.best_bid)
            pnl_pct = (b.best_bid - ctx.entry_price) / ctx.entry_price if ctx.entry_price else 0.0
            self.store.mark(tid, b.best_bid, b.best_ask, q_side, pnl_pct, m.seconds_remaining)
            if ctx.shares < m.min_size and ctx.shares > 0:
                continue
            sell_net = b.best_bid - m.taker_fee_per_share(b.best_bid)
            hedge_cost = None
            if ob is not None and ob.best_ask is not None and ctx.hedge_shares <= 0:
                hedge_cost = ob.best_ask + m.taker_fee_per_share(ob.best_ask)
            cash_other = max(1.0, self.equity - ctx.shares * (b.mid or ctx.entry_price))
            # Exit only if it is still +growth when our own estimate is wrong by `exit_conservative_sigmas`
            # in the position's favour: this stops the policy from churning on model noise.
            q_cons = min(0.999, q_side + self.cfg.exit_conservative_sigmas *
                         q_std(q_side, self.params.n_fit, self.cfg.model_uncertainty_floor))
            d = decide(q_cons, cash_other, ctx.shares, sell_net, hedge_cost, m.min_size, self.cfg.exit_min_improvement)
            if d.action == "hold":
                continue
            ctx.reason_close = (f"{d.reason}; q={q_side:.3f} bid={b.best_bid:.3f} pnl={pnl_pct:+.1%} "
                                f"t-{m.seconds_remaining:.0f}s mom_z={est.mom_z if est else 0:+.2f}")
            log.info("EXIT DECISION %s %s -> %s %.0f sh: %s", m.slug, ctx.side, d.action, d.shares, ctx.reason_close)
            if d.action == "sell":
                # try to earn the spread first when there is time, otherwise take
                if m.seconds_remaining > 45 and b.best_ask is not None and b.best_ask - b.best_bid > m.tick + 1e-9:
                    px = round(b.best_ask - m.tick, 2)
                    o = self.broker.limit_order(ctx.token, "SELL", px, d.shares, True, m.fee_rate, m.condition_id, ctx.side)
                    if o.status != "cancelled":
                        self.pending[o.order_id] = Pending(o, m, ctx.side, "exit", est, None, 10.0, ctx); continue
                o = self.broker.market_order(ctx.token, "SELL", d.shares, m.fee_rate, m.condition_id, ctx.side)
                if o.filled > 0:
                    self.apply_fill(Pending(o, m, ctx.side, "exit", est, None, 0, ctx), o)
            else:
                o = self.broker.market_order(opp_token, "BUY", d.shares, m.fee_rate, m.condition_id,
                                             "Down" if ctx.side == "Up" else "Up")
                if o.filled > 0:
                    self.apply_fill(Pending(o, m, ctx.side, "hedge", est, None, 0, ctx), o)

    def flatten(self):
        for oid, p in list(self.pending.items()):
            if p.purpose == "entry":
                self.broker.cancel(p.order); del self.pending[oid]
        for tid, ctx in list(self.trades.items()):
            if any(p.trade is ctx for p in self.pending.values()) or ctx.shares <= 0:
                continue
            m = ctx.market
            ctx.reason_close = "flatten requested by operator"
            o = self.broker.market_order(ctx.token, "SELL", ctx.shares, m.fee_rate, m.condition_id, ctx.side)
            if o.filled > 0:
                self.apply_fill(Pending(o, m, ctx.side, "exit", None, None, 0, ctx), o)

    # ------------------------------------------------------------------ entries
    def scan_entries(self, now: float, live: bool):
        for mid, m in list(self.markets.items()):
            rem = m.end_ts - now
            if not (self.cfg.min_seconds_remaining <= rem <= self.cfg.max_seconds_remaining) or m.closed:
                continue
            if any(c.market.market_id == mid for c in self.trades.values()) or \
               any(p.market.market_id == mid for p in self.pending.values()):
                continue
            bu, bd = self.book(m.up_token), self.book(m.down_token)
            if bu is None or bd is None or bu.mid is None or bd.mid is None:
                continue
            p_up = 0.5 * (bu.mid + (1 - bd.mid))
            est = self.model.estimate(m, p_up, now)
            if est is None:
                continue
            for side, book in (("Up", bu), ("Down", bd)):
                q = est.q if side == "Up" else 1 - est.q
                if book.best_bid is None or book.best_ask is None:
                    continue
                maker_ok = self.cfg.prefer_maker and rem > self.cfg.entry_ttl_seconds + self.cfg.min_seconds_remaining
                options = []
                if maker_ok:
                    options.append(("maker", book.best_bid, 0.0))
                if self.cfg.allow_taker:
                    options.append(("taker", book.best_ask, m.taker_fee_per_share(book.best_ask)))
                best = None
                for kind, px, fee in options:
                    sz = size_position(q, px, fee, self.equity, self.cfg, self.params.n_fit, self.exposure_fraction(), m.min_size)
                    if sz.shares > 0 and (best is None or sz.growth > best[1].growth):
                        best = ((kind, px, fee), sz)
                feats = dict(est.features, side=side)
                stub = {"asset": m.asset, "interval": m.interval, "edge": (best[1].edge if best else q - book.best_ask),
                        "side": side, "features": feats}
                blocked = [k for k in bucket_keys(stub) if k in self.blocked]
                skip = None
                if best is None:
                    skip = "no positive-growth size"
                elif blocked:
                    skip = "blocked bucket " + ",".join(blocked)
                elif len(self.trades) + len(self.pending) >= self.cfg.max_positions:
                    skip = "max positions"
                elif not live:
                    skip = "entries halted: " + (self.day.status if not self.day.entries_allowed else self.state.reason or "operator")
                if skip and best is None and (q - book.best_ask) < self.cfg.min_edge:
                    continue   # not even a signal - don't spam the journal
                if self.store.has_signal(mid, side):
                    continue
                sid = self.store.log_signal(market_id=mid, asset=m.asset, interval=m.interval, side=side, q_est=q,
                                            q_model=est.q_model, p_market=est.p_market, edge=stub["edge"],
                                            taken=0, skip_reason=skip, features=feats, params_version=self.params_version)
                if skip:
                    log.info("signal %s %s q=%.3f edge=%+.3f -> skipped (%s)", m.slug, side, q, stub["edge"], skip)
                    continue
                (kind, px, fee), sz = best
                tok = m.token_for(side)
                if kind == "maker":
                    o = self.broker.limit_order(tok, "BUY", px, sz.shares, True, m.fee_rate, m.condition_id, side)
                else:
                    o = self.broker.market_order(tok, "BUY", sz.shares, m.fee_rate, m.condition_id, side)
                if o.status == "cancelled" and o.filled <= 0:
                    err = getattr(o, "error", "")
                    if err:
                        log.warning("order REJECTED by exchange for %s %s: %s", m.slug, side, err)
                        self.store.event("order_rejected", f"{m.slug} {side}: {err}")
                    continue
                p = Pending(o, m, side, "entry", est, sz, self.cfg.entry_ttl_seconds, signal_id=sid)
                p.reason = (f"{kind} q={q:.3f} vs cost {px+fee:.3f}: edge {sz.edge:+.3f}, z={est.z:+.2f}, "
                            f"sigma={est.sigma_per_sec*math.sqrt(est.tau_eff):.4f}, mom_z={est.mom_z:+.2f}, {sz.reason}")
                self.pending[o.order_id] = p
                log.info("ENTRY %s %s %s %.0f sh @ %.3f  q=%.3f edge=%+.3f f=%.3f (full %.3f x k %.2f)", m.slug, side, kind,
                         sz.shares, px, q, sz.edge, sz.fraction, sz.f_full, sz.k_dd)
                if o.filled > 0:
                    self.open_trade(p, o)
                break   # one side per market

    # ------------------------------------------------------------------ learning
    def learn(self, why: str):
        self.resolved_since_learn = 0
        self.resolved_signals_since_learn = 0
        sigs = self.store.resolved_signals()
        res = propose_update(sigs, self.params, self.cfg)
        log.info("learning (%s): %s", why, res.note)
        self.store.event("learn", f"{why}: {res.note}")
        if res.accepted and res.new_params:
            self.params = res.new_params
            self.model.params = self.params
            self.params_version = self.store.save_params(self.params.to_dict(), res.note, res.n, res.val_ll_new)
            log.info("model params -> v%d %s", self.params_version, self.params.to_dict())
        trades = self.store.resolved_trades()
        if trades:
            bl = blocked_buckets(bucket_stats(trades), self.cfg.block_bucket_min_n)
            self.store.set_blocked(bl); self.blocked = set(bl)
            if bl:
                log.info("blocked buckets: %s", bl)

    # ------------------------------------------------------------------ status
    def write_status(self, can_enter: bool):
        d = self.day.day
        write_status(running=True, mode=self.mode, equity=round(self.equity, 2), day=d.date,
                     day_start_equity=round(d.start_equity, 2),
                     day_pnl_pct=round((self.equity / d.start_equity - 1) * 100, 2) if d.start_equity else 0,
                     day_realized=round(d.realized_pnl, 2), day_trades=d.trades, day_status=self.day.status,
                     entries_allowed=can_enter, control=self.state.reason, feed_connected=self.feed.connected,
                     open_positions=[{"market": c.market.slug, "side": c.side, "shares": c.shares,
                                      "entry": c.entry_price, "mgmt": c.management} for c in self.trades.values()],
                     pending_orders=len(self.pending), markets_tracked=len(self.markets),
                     params_version=self.params_version, blocked=sorted(self.blocked), clock_offset_s=round(clock.offset(), 1))
