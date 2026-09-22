"""Order execution. LiveBroker wraps py-clob-client; PaperBroker simulates fills against the live book.

Fee model (Polymarket crypto markets): only takers pay, fee_usdc = shares * rate * p * (1-p).
"""
from __future__ import annotations
import math
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional
import requests

CLOB = "https://clob.polymarket.com"
DATA_API = "https://data-api.polymarket.com"


@dataclass
class Book:
    token: str
    bids: List[tuple]   # (price, size) best first
    asks: List[tuple]
    ts: float

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        if self.best_bid is None or self.best_ask is None:
            return self.best_bid or self.best_ask
        return (self.best_bid + self.best_ask) / 2

    def depth_price(self, side: str, shares: float) -> Optional[float]:
        """Average fill price for `shares` taking liquidity on `side` ('BUY' eats asks)."""
        levels = self.asks if side == "BUY" else self.bids
        need, cost = shares, 0.0
        for p, s in levels:
            take = min(need, s)
            cost += take * p
            need -= take
            if need <= 1e-9:
                return cost / shares
        return None


@dataclass
class Order:
    order_id: str
    token: str
    side: str
    price: float
    size: float
    filled: float = 0.0
    avg_fill: float = 0.0
    fee_paid: float = 0.0
    status: str = "open"      # open | filled | partial | cancelled
    maker: bool = True
    error: str = ""
    created: float = field(default_factory=time.time)


@dataclass
class Position:
    token: str
    condition_id: str
    outcome: str
    shares: float
    avg_price: float
    fees_paid: float = 0.0


def fetch_book(token: str, timeout: float = 6.0) -> Book:
    r = requests.get(f"{CLOB}/book", params={"token_id": token}, timeout=timeout)
    r.raise_for_status()
    d = r.json()
    bids = sorted(((float(x["price"]), float(x["size"])) for x in d.get("bids", [])), key=lambda t: -t[0])
    asks = sorted(((float(x["price"]), float(x["size"])) for x in d.get("asks", [])), key=lambda t: t[0])
    return Book(token, bids, asks, time.time())


class Broker:
    """Interface. Prices are per share (0..1); sizes are shares."""
    name = "base"

    def cash(self) -> float: ...
    def positions(self) -> Dict[str, Position]: ...
    def book(self, token: str) -> Book: return fetch_book(token)
    def limit_order(self, token: str, side: str, price: float, size: float, post_only: bool, fee_rate: float,
                    condition_id: str = "", outcome: str = "") -> Order: ...
    def market_order(self, token: str, side: str, size: float, fee_rate: float,
                     condition_id: str = "", outcome: str = "") -> Order: ...
    def refresh_order(self, order: Order) -> Order: ...
    def cancel(self, order: Order) -> None: ...
    def cancel_all(self) -> None: ...
    def settle(self, condition_id: str, outcome: str) -> float:
        """Called when a market resolves; returns realized cash credited (paper) or 0 (live: on-chain)."""
        return 0.0


# --------------------------------------------------------------------------------------- paper

class PaperBroker(Broker):
    name = "paper"

    def __init__(self, cash: float):
        self._cash = cash
        self._pos: Dict[str, Position] = {}
        self._orders: Dict[str, Order] = {}

    def cash(self) -> float:
        return self._cash

    def positions(self) -> Dict[str, Position]:
        return {k: v for k, v in self._pos.items() if v.shares > 1e-9}

    def _fill(self, o: Order, price: float, size: float, taker: bool, fee_rate: float, condition_id: str, outcome: str):
        fee = size * fee_rate * price * (1 - price) if taker else 0.0
        if o.side == "BUY":
            cost = size * price + fee
            if cost > self._cash + 1e-9:
                size = max(0.0, (self._cash - fee) / price)
                cost = size * price + fee
            if size <= 0:
                return
            self._cash -= cost
            p = self._pos.get(o.token)
            if p is None:
                self._pos[o.token] = Position(o.token, condition_id, outcome, size, price, fee)
            else:
                tot = p.shares + size
                p.avg_price = (p.avg_price * p.shares + price * size) / tot
                p.shares = tot
                p.fees_paid += fee
        else:
            p = self._pos.get(o.token)
            if p is None or p.shares <= 0:
                return
            size = min(size, p.shares)
            self._cash += size * price - fee
            p.shares -= size
            p.fees_paid += fee
        o.avg_fill = (o.avg_fill * o.filled + price * size) / (o.filled + size)
        o.filled += size
        o.fee_paid += fee
        o.status = "filled" if o.filled >= o.size - 1e-9 else "partial"

    def limit_order(self, token, side, price, size, post_only, fee_rate, condition_id="", outcome=""):
        o = Order(str(uuid.uuid4()), token, side, price, size, maker=True)
        o.meta = (fee_rate, condition_id, outcome)
        self._orders[o.order_id] = o
        book = self.book(token)
        crosses = (side == "BUY" and book.best_ask is not None and book.best_ask <= price) or \
                  (side == "SELL" and book.best_bid is not None and book.best_bid >= price)
        if crosses:
            if post_only:
                o.status = "cancelled"   # exchange would reject a post-only order that crosses
            else:
                fp = book.depth_price(side, size) or (book.best_ask if side == "BUY" else book.best_bid)
                o.maker = False
                self._fill(o, fp, size, True, fee_rate, condition_id, outcome)
        return o

    def market_order(self, token, side, size, fee_rate, condition_id="", outcome=""):
        o = Order(str(uuid.uuid4()), token, side, 0.0, size, maker=False)
        book = self.book(token)
        fp = book.depth_price(side, size)
        if fp is None:
            o.status = "cancelled"
            return o
        o.price = fp
        self._fill(o, fp, size, True, fee_rate, condition_id, outcome)
        return o

    def refresh_order(self, o: Order) -> Order:
        """Simulate a resting maker order: fills when the far side trades through our price."""
        if o.status in ("filled", "cancelled"):
            return o
        book = self.book(o.token)
        fee_rate, cid, outcome = getattr(o, "meta", (0.0, "", ""))
        remaining = o.size - o.filled
        if o.side == "BUY" and book.best_ask is not None and book.best_ask <= o.price:
            self._fill(o, o.price, remaining, False, fee_rate, cid, outcome)
        elif o.side == "SELL" and book.best_bid is not None and book.best_bid >= o.price:
            self._fill(o, o.price, remaining, False, fee_rate, cid, outcome)
        elif time.time() - o.created > 8 and book.mid is not None and abs(book.mid - o.price) <= 0.011:
            # queue-position heuristic: at/near the touch for >8s, assume we get filled
            self._fill(o, o.price, remaining, False, fee_rate, cid, outcome)
        return o

    def cancel(self, o: Order) -> None:
        if o.status not in ("filled",):
            o.status = "cancelled"

    def cancel_all(self) -> None:
        for o in self._orders.values():
            self.cancel(o)

    def settle(self, condition_id: str, outcome: str) -> float:
        credited = 0.0
        for tok, p in list(self._pos.items()):
            if p.condition_id == condition_id and p.shares > 0:
                if p.outcome == outcome:
                    credited += p.shares
                p.shares = 0.0
        self._cash += credited
        return credited


# ---------------------------------------------------------------------------------------- live

class LiveBroker(Broker):
    """Live trading via py-clob-client-v2, which supports every Polymarket wallet type:
       0 = EOA, 1 = legacy Magic proxy, 2 = legacy Gnosis Safe, 3 = Deposit Wallet (POLY_1271).
    Accounts created from May 2026 onward are Deposit Wallets and REQUIRE signature type 3;
    the older py-clob-client only knows types 0-2."""
    name = "live"

    def __init__(self, private_key: str, funder: str, signature_type: int):
        from py_clob_client_v2.client import ClobClient
        from py_clob_client_v2.order_utils.model.signature_type_v2 import SignatureTypeV2
        self.sig_type = SignatureTypeV2(int(signature_type))
        self.client = ClobClient(CLOB, chain_id=137, key=private_key,
                                 signature_type=self.sig_type, funder=funder)
        self.client.set_api_creds(self.client.create_or_derive_api_key())
        self.funder = funder
        self._pos_cache: tuple[float, Dict[str, Position]] = (0.0, {})
        self._tick_cache: Dict[str, float] = {}

    # -------------------------------------------------------------- account
    def cash(self) -> float:
        from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
        r = self.client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
        return float(r.get("balance", 0)) / 1e6

    def positions(self) -> Dict[str, Position]:
        now = time.time()
        if now - self._pos_cache[0] < 3:
            return self._pos_cache[1]
        r = requests.get(f"{DATA_API}/positions", params={"user": self.funder, "sizeThreshold": 0, "limit": 500},
                         timeout=8)
        r.raise_for_status()
        out = {}
        for p in r.json():
            if float(p.get("size", 0)) <= 0 or p.get("redeemable"):
                continue
            out[p["asset"]] = Position(p["asset"], p["conditionId"], p.get("outcome", ""), float(p["size"]),
                                       float(p.get("avgPrice", 0)), float(p.get("entryFeesUsdc", 0) or 0))
        self._pos_cache = (now, out)
        return out

    # -------------------------------------------------------------- orders
    def _tick(self, token: str) -> float:
        if token not in self._tick_cache:
            try:
                self._tick_cache[token] = float(self.client.get_tick_size(token))
            except Exception:
                self._tick_cache[token] = 0.01
        return self._tick_cache[token]

    def _wrap(self, resp: dict, token, side, price, size, maker) -> Order:
        resp = resp or {}
        oid = resp.get("orderID") or resp.get("orderId") or resp.get("id") or ""
        o = Order(oid, token, side, price, size, maker=maker)
        if not resp.get("success", True) or not oid:
            o.status = "cancelled"
            o.error = str(resp.get("errorMsg") or resp.get("error") or "")[:200]
        return o

    def limit_order(self, token, side, price, size, post_only, fee_rate, condition_id="", outcome=""):
        from py_clob_client_v2.clob_types import OrderArgsV2, OrderType
        tick = self._tick(token)
        decimals = max(1, round(-math.log10(tick)))
        price = round(math.floor(price / tick + 1e-9) * tick if side == "BUY"
                      else math.ceil(price / tick - 1e-9) * tick, decimals)
        price = min(1 - tick, max(tick, price))
        args = OrderArgsV2(token_id=token, price=price, size=round(size, 2), side=side)
        signed = self.client.create_order(args)
        resp = self.client.post_order(signed, OrderType.GTC, post_only=post_only)
        return self._wrap(resp, token, side, price, size, maker=True)

    def market_order(self, token, side, size, fee_rate, condition_id="", outcome=""):
        from py_clob_client_v2.clob_types import MarketOrderArgsV2, OrderType
        # amount is USDC to spend for BUY, shares to sell for SELL
        if side == "BUY":
            book = self.book(token)
            px = book.depth_price("BUY", size) or book.best_ask or 0.99
            amount = round(size * px * (1 + fee_rate * px * (1 - px)) + 0.01, 2)
        else:
            amount = round(size, 2)
        args = MarketOrderArgsV2(token_id=token, amount=amount, side=side, order_type=OrderType.FOK)
        signed = self.client.create_market_order(args)
        resp = self.client.post_order(signed, OrderType.FOK)
        o = self._wrap(resp, token, side, 0.0, size, maker=False)
        if o.status == "cancelled" and o.error:
            import logging; logging.getLogger("polybot").warning("market order rejected: %s", o.error)
        return self.refresh_order(o) if o.status != "cancelled" else o

    def refresh_order(self, o: Order) -> Order:
        if not o.order_id or o.status in ("filled", "cancelled"):
            return o
        try:
            d = self.client.get_order(o.order_id) or {}
        except Exception:
            return o
        size = float(d.get("original_size") or o.size)
        matched = float(d.get("size_matched") or 0)
        o.filled = matched
        o.avg_fill = float(d.get("price") or o.price)
        st = (d.get("status") or "").lower()
        if matched >= size - 1e-6 or st == "matched":
            o.status = "filled"
        elif st in ("cancelled", "canceled", "expired"):
            o.status = "cancelled" if matched <= 0 else "partial"
        elif matched > 0:
            o.status = "partial"
        return o

    def cancel(self, o: Order) -> None:
        from py_clob_client_v2.clob_types import OrderPayload
        if o.order_id:
            try:
                self.client.cancel_order(OrderPayload(orderID=o.order_id))
            except Exception:
                pass
        if o.status == "open":
            o.status = "cancelled"

    def cancel_all(self) -> None:
        try:
            self.client.cancel_all()
        except Exception:
            pass
