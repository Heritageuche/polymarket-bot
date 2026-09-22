"""SQLite journal: every trade, every mark-to-market sample, every signal (taken or not), every
parameter version, every control event."""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable, List, Optional
from ..config import DATA_DIR
from .. import clock

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY, ts_open REAL, ts_close REAL, mode TEXT,
  market_id TEXT, condition_id TEXT, slug TEXT, asset TEXT, interval TEXT, outcome_side TEXT, token TEXT,
  entry_price REAL, entry_fee REAL, exit_price REAL, resolution_price REAL,
  shares REAL, stake_usd REAL, fraction REAL, kelly_full REAL, kelly_shrunk REAL, kelly_mult REAL,
  q_est REAL, q_model REAL, p_market REAL, edge REAL, ev_usd REAL, var_usd REAL,
  outcome TEXT, won INTEGER, pnl REAL, max_drawdown_pct REAL, min_mark REAL, max_mark REAL,
  sigma REAL, tau_entry REAL, mom_z REAL, abs_z REAL, spread REAL, strike REAL, spot_entry REAL,
  signal_strength REAL, maker INTEGER, management TEXT, reason_open TEXT, reason_close TEXT,
  params_version INTEGER, features TEXT, status TEXT DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS marks (
  trade_id INTEGER, ts REAL, bid REAL, ask REAL, q REAL, pnl_pct REAL, seconds_remaining REAL
);
CREATE INDEX IF NOT EXISTS marks_trade ON marks(trade_id);
CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY, ts REAL, market_id TEXT, asset TEXT, interval TEXT, side TEXT,
  q_est REAL, q_model REAL, p_market REAL, edge REAL, taken INTEGER, skip_reason TEXT,
  outcome TEXT, won INTEGER, features TEXT, params_version INTEGER
);
CREATE INDEX IF NOT EXISTS signals_market ON signals(market_id);
CREATE TABLE IF NOT EXISTS params (
  version INTEGER PRIMARY KEY, ts REAL, params TEXT, note TEXT, n_trades INTEGER, val_logloss REAL
);
CREATE TABLE IF NOT EXISTS days (
  date TEXT PRIMARY KEY, start_equity REAL, end_equity REAL, trades INTEGER, realized_pnl REAL,
  target_hit INTEGER, loss_halt INTEGER, halt_reason TEXT
);
CREATE TABLE IF NOT EXISTS events (ts REAL, kind TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS blocked (bucket TEXT PRIMARY KEY, ts REAL, n INTEGER, roi REAL, ucb REAL);
"""


class Store:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or DATA_DIR / "polybot.db"
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    # ---------------------------------------------------------------- trades
    def open_trade(self, **f) -> int:
        f.setdefault("ts_open", clock.now()); f.setdefault("status", "open")
        if isinstance(f.get("features"), dict):
            f["features"] = json.dumps(f["features"])
        cols = ", ".join(f); qs = ", ".join("?" for _ in f)
        cur = self.db.execute(f"INSERT INTO trades ({cols}) VALUES ({qs})", list(f.values()))
        self.db.commit()
        return cur.lastrowid

    def update_trade(self, trade_id: int, **f):
        if isinstance(f.get("features"), dict):
            f["features"] = json.dumps(f["features"])
        sets = ", ".join(f"{k}=?" for k in f)
        self.db.execute(f"UPDATE trades SET {sets} WHERE id=?", list(f.values()) + [trade_id])
        self.db.commit()

    def mark(self, trade_id: int, bid, ask, q, pnl_pct, seconds_remaining):
        self.db.execute("INSERT INTO marks VALUES (?,?,?,?,?,?,?)",
                        (trade_id, clock.now(), bid, ask, q, pnl_pct, seconds_remaining))
        self.db.commit()

    def close_flat_trades(self, market_id: str, outcome: str):
        self.db.execute("UPDATE trades SET status='closed', outcome=?, won=(outcome_side=?), ts_close=COALESCE(ts_close, ?) "
                        "WHERE market_id=? AND status='open_flat'", (outcome, outcome, clock.now(), market_id))
        self.db.commit()

    def open_trades(self) -> List[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades WHERE status='open'").fetchall()

    def resolved_trades(self, limit: int = 100000) -> List[sqlite3.Row]:
        return self.db.execute("SELECT * FROM trades WHERE status='closed' AND won IS NOT NULL ORDER BY ts_close LIMIT ?",
                               (limit,)).fetchall()

    def resolved_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM trades WHERE status='closed' AND won IS NOT NULL").fetchone()[0]

    def marks_for(self, trade_id: int):
        return self.db.execute("SELECT * FROM marks WHERE trade_id=? ORDER BY ts", (trade_id,)).fetchall()

    # ---------------------------------------------------------------- signals (taken or shadow)
    def log_signal(self, **f) -> int:
        f.setdefault("ts", clock.now())
        if isinstance(f.get("features"), dict):
            f["features"] = json.dumps(f["features"])
        cols = ", ".join(f); qs = ", ".join("?" for _ in f)
        cur = self.db.execute(f"INSERT INTO signals ({cols}) VALUES ({qs})", list(f.values()))
        self.db.commit()
        return cur.lastrowid

    def resolve_signals(self, market_id: str, outcome: str) -> int:
        cur = self.db.execute("UPDATE signals SET outcome=?, won=(side=?) WHERE market_id=? AND outcome IS NULL",
                              (outcome, outcome, market_id))
        self.db.commit()
        return cur.rowcount or 0

    def resolved_signals(self):
        return self.db.execute("SELECT * FROM signals WHERE won IS NOT NULL ORDER BY ts").fetchall()

    def has_signal(self, market_id: str, side: str, within: float = 60.0) -> bool:
        r = self.db.execute("SELECT 1 FROM signals WHERE market_id=? AND side=? AND ts>? LIMIT 1",
                            (market_id, side, clock.now() - within)).fetchone()
        return r is not None

    # ---------------------------------------------------------------- params
    def latest_params(self):
        return self.db.execute("SELECT * FROM params ORDER BY version DESC LIMIT 1").fetchone()

    def save_params(self, params: dict, note: str, n: int, val_logloss: float) -> int:
        row = self.latest_params()
        v = (row["version"] + 1) if row else 1
        self.db.execute("INSERT INTO params VALUES (?,?,?,?,?,?)", (v, clock.now(), json.dumps(params), note, n, val_logloss))
        self.db.commit()
        return v

    # ---------------------------------------------------------------- misc
    def log_day(self, day, end_equity: float):
        self.db.execute("INSERT OR REPLACE INTO days VALUES (?,?,?,?,?,?,?,?)",
                        (day.date, day.start_equity, end_equity, day.trades, day.realized_pnl,
                         int(day.target_hit), int(day.loss_halt), day.halt_reason))
        self.db.commit()

    def event(self, kind: str, detail: str = ""):
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (clock.now(), kind, detail))
        self.db.commit()

    def set_blocked(self, buckets: dict):
        self.db.execute("DELETE FROM blocked")
        for b, (n, roi, ucb) in buckets.items():
            self.db.execute("INSERT INTO blocked VALUES (?,?,?,?,?)", (b, clock.now(), n, roi, ucb))
        self.db.commit()

    def blocked(self) -> set:
        return {r["bucket"] for r in self.db.execute("SELECT bucket FROM blocked")}
