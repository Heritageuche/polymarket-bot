"""Exchange-synced clock. The host clock cannot be trusted for market timing (this machine was found
running 8 hours ahead of real time); every window start/end comparison uses `now()`."""
from __future__ import annotations
import logging
import threading
import time
import requests

log = logging.getLogger("polybot.clock")
_offset = 0.0          # local - server, seconds
_last_sync = 0.0
_lock = threading.Lock()
SYNC_EVERY = 600.0


def _server_time() -> float | None:
    for url, key in (("https://api.binance.com/api/v3/time", "serverTime"),
                     ("https://api.exchange.coinbase.com/time", "epoch")):
        try:
            v = requests.get(url, timeout=6).json()[key]
            return float(v) / (1000.0 if key == "serverTime" else 1.0)
        except Exception:
            continue
    return None


def sync(force: bool = False) -> float:
    global _offset, _last_sync
    with _lock:
        if not force and time.time() - _last_sync < SYNC_EVERY:
            return _offset
        t0 = time.time()
        st = _server_time()
        t1 = time.time()
        if st is not None:
            new = (t0 + t1) / 2 - st
            if abs(new - _offset) > 2 and _last_sync:
                log.warning("clock offset changed %+.1fs -> %+.1fs", _offset, new)
            _offset = new
            _last_sync = t1
            if abs(_offset) > 2:
                log.warning("host clock is %+.1f s off real time; using exchange time", _offset)
        elif not _last_sync:
            log.error("could not sync clock with any exchange; falling back to host clock")
            _last_sync = t1
        return _offset


def now() -> float:
    if time.time() - _last_sync >= SYNC_EVERY:
        sync()
    return time.time() - _offset


def offset() -> float:
    return _offset
