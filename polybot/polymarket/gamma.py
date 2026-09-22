"""Market discovery via the Gamma API (public, no auth)."""
from __future__ import annotations
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional
import requests
from .. import clock

GAMMA = "https://gamma-api.polymarket.com"

CHAINLINK_SYMBOL = {"btc": "btc/usd", "eth": "eth/usd", "sol": "sol/usd", "xrp": "xrp/usd",
                    "doge": "doge/usd", "bnb": "bnb/usd", "hype": "hype/usd", "zec": "zec/usd"}
BINANCE_SYMBOL = {"btc": "btcusdt", "eth": "ethusdt", "sol": "solusdt", "xrp": "xrpusdt",
                  "doge": "dogeusdt", "bnb": "bnbusdt", "hype": "hypeusdt", "zec": "zecusdt"}
HOURLY_SERIES = {"btc": "btc-up-or-down-hourly", "eth": "ethereum-up-or-down-hourly",
                 "sol": "solana-up-or-down-hourly", "xrp": "xrp-up-or-down-hourly"}
DAILY_SERIES = {"btc": "btc-up-or-down-daily", "eth": "eth-up-or-down-daily"}
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def series_slug(asset: str, interval: str) -> Optional[str]:
    if interval in ("5m", "15m", "4h"):
        return f"{asset}-up-or-down-{interval}"
    if interval == "1h":
        return HOURLY_SERIES.get(asset)
    if interval == "1d":
        return DAILY_SERIES.get(asset)
    return None


def _iso(ts: str | None) -> Optional[float]:
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


@dataclass
class Market:
    market_id: str
    condition_id: str
    slug: str
    question: str
    asset: str
    interval: str
    up_token: str
    down_token: str
    start_ts: float
    end_ts: float
    fee_rate: float          # taker fee rate (0.07 => fee = shares*rate*p*(1-p))
    min_size: float
    tick: float
    resolution: str          # 'chainlink_twap' | 'binance_candle'
    twap_lookback: int = 60
    closed: bool = False
    accepting_orders: bool = True
    outcome_prices: List[float] = field(default_factory=list)
    best_bid: float | None = None
    best_ask: float | None = None
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def seconds_remaining(self) -> float:
        return self.end_ts - clock.now()

    @property
    def window_seconds(self) -> float:
        return self.end_ts - self.start_ts

    @property
    def resolved_outcome(self) -> Optional[str]:
        """'Up' / 'Down' once Gamma reports final prices, else None."""
        if len(self.outcome_prices) == 2 and self.closed:
            if self.outcome_prices[0] >= 0.99:
                return "Up"
            if self.outcome_prices[1] >= 0.99:
                return "Down"
        return None

    def token_for(self, outcome: str) -> str:
        return self.up_token if outcome == "Up" else self.down_token

    def taker_fee_per_share(self, price: float) -> float:
        return self.fee_rate * price * (1.0 - price)


def parse_market(m: dict, asset: str, interval: str) -> Optional[Market]:
    try:
        tokens = json.loads(m.get("clobTokenIds") or "[]")
        outcomes = json.loads(m.get("outcomes") or "[]")
        if len(tokens) != 2 or [o.lower() for o in outcomes] != ["up", "down"]:
            return None
        start = _iso(m.get("eventStartTime"))
        end = _iso(m.get("endDate"))
        if start is None:
            start = (end or 0) - INTERVAL_SECONDS[interval]
        if end is None:
            return None
        fees = m.get("feeSchedule") or {}
        fee_rate = float(fees.get("rate") or 0.0) if m.get("feesEnabled", True) else 0.0
        src = (m.get("resolutionSource") or "").lower()
        cfg = m.get("cryptoMarketConfig") or {}
        resolution = "chainlink_twap" if "chain.link" in src else "binance_candle"
        prices = []
        try:
            prices = [float(x) for x in json.loads(m.get("outcomePrices") or "[]")]
        except Exception:
            pass
        return Market(
            market_id=str(m.get("id")), condition_id=m.get("conditionId", ""), slug=m.get("slug", ""),
            question=m.get("question", ""), asset=asset, interval=interval,
            up_token=tokens[0], down_token=tokens[1], start_ts=start, end_ts=end,
            fee_rate=fee_rate, min_size=float(m.get("orderMinSize") or 5),
            tick=float(m.get("orderPriceMinTickSize") or 0.01), resolution=resolution,
            twap_lookback=int(cfg.get("twapLookbackSeconds") or 60) if cfg.get("twapEnabled", True) else 0,
            closed=bool(m.get("closed")), accepting_orders=bool(m.get("acceptingOrders", True)),
            outcome_prices=prices, best_bid=m.get("bestBid"), best_ask=m.get("bestAsk"), raw=m,
        )
    except Exception:
        return None


class Gamma:
    def __init__(self, session: requests.Session | None = None, timeout: float = 10.0):
        self.s = session or requests.Session()
        self.timeout = timeout

    def _get(self, path: str, **params):
        r = self.s.get(GAMMA + path, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def live_markets(self, assets: List[str], intervals: List[str]) -> List[Market]:
        out: List[Market] = []
        for asset in assets:
            for interval in intervals:
                slug = series_slug(asset, interval)
                if not slug:
                    continue
                try:
                    # newest first: Gamma also returns stale never-resolved windows from months ago,
                    # so order by start and keep only windows that end in the near future.
                    since = datetime.fromtimestamp(clock.now() - 120, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                    events = self._get("/events", series_slug=slug, active="true", closed="false", limit=12,
                                       order="endDate", ascending="true", end_date_min=since)
                except Exception:
                    continue
                now = clock.now()
                for e in events:
                    for m in e.get("markets", []):
                        mk = parse_market(m, asset, interval)
                        if mk and now - 120 < mk.end_ts < now + 2 * 86400:
                            out.append(mk)
        return out

    def refresh(self, market: Market) -> Market:
        """Re-fetch one market (used to detect resolution)."""
        m = self._get(f"/markets/{market.market_id}")
        upd = parse_market(m, market.asset, market.interval)
        return upd or market
