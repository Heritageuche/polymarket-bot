"""Price feed: Polymarket RTDS websocket (Chainlink 1-second ticks = the resolution source for
5m/15m/4h markets, plus Binance ticks) with Binance REST klines as fallback (the resolution source for
hourly markets)."""
from __future__ import annotations
import asyncio
import json
import math
import threading
import time
from bisect import bisect_left
from collections import deque
from typing import Deque, Dict, Optional, Tuple
import requests
import websockets

RTDS = "wss://ws-live-data.polymarket.com"
BINANCE = "https://api.binance.com/api/v3/klines"
MAX_TICKS = 4 * 3600  # keep ~4h of 1-second ticks per symbol
STALL_SECONDS = 60.0      # a quiet oracle is normal; only treat a silence this long as a dead socket
MIN_RECONNECT_GAP = 3.0   # floor on reconnect spacing
MAX_PRICE_AGE = 45.0      # refuse to price a market on data older than this


class Series:
    def __init__(self):
        self.ts: Deque[float] = deque(maxlen=MAX_TICKS)
        self.px: Deque[float] = deque(maxlen=MAX_TICKS)
        self.ewma_var: float = 0.0        # per-second variance of log returns
        self.n: int = 0
        self.lam = math.exp(-math.log(2) / 300.0)   # 5-minute half-life
        self.last_ts: float = 0.0

    def add(self, ts: float, px: float):
        if px <= 0 or (self.ts and ts <= self.ts[-1]):
            return
        if self.px:
            dt = max(1.0, ts - self.ts[-1])
            r = math.log(px / self.px[-1])
            v = r * r / dt
            self.ewma_var = v if self.n == 0 else self.lam * self.ewma_var + (1 - self.lam) * v
            self.n += 1
        self.ts.append(ts); self.px.append(px); self.last_ts = ts

    def price(self) -> Optional[float]:
        return self.px[-1] if self.px else None

    def at(self, ts: float, tolerance: float = 5.0) -> Optional[float]:
        """First tick at or after ts (how Polymarket defines the window open)."""
        if not self.ts:
            return None
        i = bisect_left(self.ts, ts)
        if i < len(self.ts) and self.ts[i] - ts <= tolerance:
            return self.px[i]
        return None

    def twap(self, t0: float, t1: float) -> Optional[float]:
        if not self.ts:
            return None
        i = bisect_left(self.ts, t0); j = bisect_left(self.ts, t1)
        if j <= i:
            return None
        seg = list(self.px)[i:j]
        return sum(seg) / len(seg)

    def log_return(self, seconds: float) -> Optional[float]:
        if len(self.ts) < 2:
            return None
        p0 = self.at(self.ts[-1] - seconds, tolerance=seconds)
        return math.log(self.px[-1] / p0) if p0 else None

    def realized_var(self, seconds: float) -> Optional[float]:
        """Per-second variance from the last `seconds` of ticks (robust complement to the EWMA)."""
        if len(self.ts) < 30:
            return None
        i = bisect_left(self.ts, self.ts[-1] - seconds)
        ts = list(self.ts)[i:]; px = list(self.px)[i:]
        if len(px) < 30:
            return None
        acc = 0.0; tot = 0.0
        for k in range(1, len(px)):
            dt = max(1.0, ts[k] - ts[k - 1]); r = math.log(px[k] / px[k - 1])
            acc += r * r; tot += dt
        return acc / tot if tot > 0 else None


class Feed:
    def __init__(self, chainlink_symbols, binance_symbols):
        self.cl = {s: Series() for s in chainlink_symbols}
        self.bn = {s: Series() for s in binance_symbols}
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.connected = False
        self.last_msg = 0.0

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        self._thread = threading.Thread(target=self._run, name="rtds", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self):
        asyncio.run(self._loop())

    async def _loop(self):
        subs = [{"topic": "crypto_prices_chainlink", "type": "*", "filters": json.dumps({"symbol": s})} for s in self.cl]
        if self.bn:
            subs.append({"topic": "crypto_prices", "type": "update", "filters": ",".join(self.bn)})
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(RTDS, ping_interval=None, max_size=None) as ws:
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": subs}))
                    # Give the NEW connection its own stall budget. Carrying the previous
                    # connection's last_msg over made every reconnect die instantly and turned a
                    # single stall into an endless reconnect loop that the server then rate-limited.
                    self.last_msg = time.time()
                    self.connected = True; backoff = 1.0
                    last_ping = time.time()
                    while not self._stop.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                            self._handle(msg)
                        except asyncio.TimeoutError:
                            pass
                        if time.time() - last_ping > 4.5:
                            await ws.send("PING"); last_ping = time.time()
                        if time.time() - self.last_msg > STALL_SECONDS:
                            raise ConnectionError("feed stalled")
            except Exception:
                self.connected = False
                # never reconnect faster than MIN_RECONNECT_GAP, so a bad patch cannot hammer the server
                wait = max(backoff, MIN_RECONNECT_GAP)
                await asyncio.sleep(wait); backoff = min(30.0, backoff * 2)

    def _handle(self, raw):
        try:
            d = json.loads(raw)
        except Exception:
            return
        topic = d.get("topic"); payload = d.get("payload") or {}
        self.last_msg = time.time()
        sym = payload.get("symbol")
        with self._lock:
            if "data" in payload:
                # history backfill on subscribe. NOTE: arrives with topic "crypto_prices" even for
                # chainlink symbols, so route by symbol name, not topic.
                target = self.cl.get(sym) if sym in self.cl else self.bn.get(sym)
                if target is not None:
                    for t in payload["data"]:
                        target.add(float(t["timestamp"]) / 1000.0, float(t["value"]))
            elif topic == "crypto_prices_chainlink" and sym in self.cl:
                self.cl[sym].add(float(payload["timestamp"]) / 1000.0, float(payload["value"]))
            elif topic == "crypto_prices" and sym in self.bn:
                self.bn[sym].add(float(payload["timestamp"]) / 1000.0, float(payload["value"]))

    # ------------------------------------------------------------------ queries
    def series(self, resolution: str, cl_sym: str, bn_sym: str) -> Series:
        return self.cl[cl_sym] if resolution == "chainlink_twap" and cl_sym in self.cl else self.bn.get(bn_sym) or self.cl.get(cl_sym)

    def vol_per_sec(self, s: Series, bn_sym: str | None = None) -> Optional[float]:
        """sigma per sqrt(second): blend of the fast tick EWMA, a 30-minute tick realized estimate and a
        2-hour Binance 1-minute-candle estimate (oracle ticks are smoothed and understate true vol).
        Returns None until at least one robust component exists."""
        with self._lock:
            ew = s.ewma_var if s.n > 60 else None
            rv = s.realized_var(1800) if (s.ts and s.ts[-1] - s.ts[0] >= 300) else None
        bn = binance_minute_var(bn_sym) if bn_sym else None
        if rv is None and bn is None:
            return None
        parts = [(0.4, ew), (0.3, rv), (0.3, bn)]
        w = sum(a for a, v in parts if v is not None)
        return math.sqrt(sum(a * v for a, v in parts if v is not None) / w)

    def strike(self, s: Series, start_ts: float, bn_sym: str) -> Tuple[Optional[float], float]:
        """Window open price and its uncertainty (as a log-price std). Exact when we saw the tick."""
        with self._lock:
            k = s.at(start_ts)
        if k is not None:
            return k, 0.0
        k = binance_open_at(bn_sym, start_ts)
        # Chainlink vs Binance basis: a few bps; treat as ~4 bps 1-sigma uncertainty in log price.
        return (k, 0.0004) if k else (None, 0.0)


_kline_cache: Dict[Tuple[str, int], Tuple[float, float]] = {}
_minvar_cache: Dict[str, Tuple[float, float]] = {}


def binance_minute_var(symbol: str) -> Optional[float]:
    """Per-second log-return variance from the last 120 one-minute Binance candles (cached 5 min)."""
    hit = _minvar_cache.get(symbol)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    try:
        r = requests.get(BINANCE, params={"symbol": symbol.upper(), "interval": "1m", "limit": 121}, timeout=6)
        r.raise_for_status()
        closes = [float(k[4]) for k in r.json()]
        if len(closes) < 30:
            return None
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        v = sum(x * x for x in rets) / len(rets) / 60.0
        _minvar_cache[symbol] = (time.time(), v)
        return v
    except Exception:
        return hit[1] if hit else None


def binance_open_at(symbol: str, start_ts: float) -> Optional[float]:
    key = (symbol, int(start_ts // 60))
    hit = _kline_cache.get(key)
    if hit and time.time() - hit[0] < 3600:
        return hit[1]
    try:
        r = requests.get(BINANCE, params={"symbol": symbol.upper(), "interval": "1m",
                                          "startTime": int(start_ts * 1000), "limit": 1}, timeout=6)
        r.raise_for_status()
        rows = r.json()
        if rows:
            px = float(rows[0][1]); _kline_cache[key] = (time.time(), px); return px
    except Exception:
        pass
    return None


def binance_candle(symbol: str, interval: str, start_ts: float) -> Optional[Tuple[float, float]]:
    """(open, close) of the finished candle starting at start_ts, or None if not final yet."""
    try:
        r = requests.get(BINANCE, params={"symbol": symbol.upper(), "interval": interval,
                                          "startTime": int(start_ts * 1000), "limit": 1}, timeout=6)
        r.raise_for_status()
        rows = r.json()
        if rows and rows[0][6] / 1000.0 < time.time():
            return float(rows[0][1]), float(rows[0][4])
    except Exception:
        pass
    return None
