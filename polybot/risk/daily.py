"""Trading-day state: +10% target, loss halts. Halting only flips `entries_allowed`; nothing else in the
system changes, so management, logging and learning continue untouched."""
from __future__ import annotations
import json
import math
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from zoneinfo import ZoneInfo
from ..config import STATE_DIR
from .. import clock

DAY_FILE = STATE_DIR / "day.json"


@dataclass
class TradingDay:
    date: str = ""
    start_equity: float = 0.0
    target_hit: bool = False
    loss_halt: bool = False
    halt_reason: str = ""
    trades: int = 0
    realized_pnl: float = 0.0
    ev_sum: float = 0.0
    var_sum: float = 0.0
    hard_floor_frac: float = 0.08

    def save(self):
        DAY_FILE.write_text(json.dumps(asdict(self), indent=1))

    @classmethod
    def load(cls) -> "TradingDay":
        if DAY_FILE.exists():
            try:
                return cls(**json.loads(DAY_FILE.read_text()))
            except Exception:
                pass
        return cls()


class DayManager:
    def __init__(self, cfg, store):
        self.cfg = cfg
        self.store = store
        self.tz = ZoneInfo(cfg.day_timezone)
        self.day = TradingDay.load()

    def today(self) -> str:
        return datetime.fromtimestamp(clock.now(), self.tz).strftime("%Y-%m-%d")

    def roll_if_needed(self, equity: float) -> bool:
        """Returns True if a new day started."""
        if self.day.date == self.today() and self.day.start_equity > 0:
            return False
        if self.day.date:
            self.store.log_day(self.day, equity)
        self.day = TradingDay(date=self.today(), start_equity=equity, hard_floor_frac=self._hard_floor(equity))
        self.day.save()
        return True

    def _hard_floor(self, equity: float) -> float:
        # 3-sigma of a typical day's P/L under the model, clamped (see docs/RISK_FRAMEWORK.md)
        from ..strategy.kelly import drawdown_multiplier, kelly_fraction, uncertainty_shrink, q_std
        q, px, n = 0.58, 0.5, 20
        k = drawdown_multiplier(self.cfg.drawdown_level, self.cfg.drawdown_prob)
        f = min(k * kelly_fraction(q, px) * uncertainty_shrink(q - px, q_std(q, 0, self.cfg.model_uncertainty_floor)),
                self.cfg.max_fraction_per_trade)
        sd = math.sqrt(n * (f / px) ** 2 * q * (1 - q))
        return min(self.cfg.daily_hard_loss_cap, max(self.cfg.daily_hard_loss_floor, 3 * sd))

    def on_trade_resolved(self, pnl: float, ev: float, var: float):
        d = self.day
        d.trades += 1; d.realized_pnl += pnl; d.ev_sum += ev; d.var_sum += var
        self._check_loss_halt()
        d.save()

    def _check_loss_halt(self):
        d = self.day
        if d.loss_halt:
            return
        if d.start_equity > 0 and d.realized_pnl <= -d.hard_floor_frac * d.start_equity:
            d.loss_halt = True; d.halt_reason = f"hard floor {d.hard_floor_frac:.1%} hit"
            return
        if d.trades >= self.cfg.daily_loss_min_trades and d.var_sum > 0:
            z = (d.realized_pnl - d.ev_sum) / math.sqrt(d.var_sum)
            if z <= -self.cfg.daily_loss_z:
                d.loss_halt = True
                d.halt_reason = f"today's P/L is {z:.1f} sigma below the model's expectation -> model likely wrong today"

    def update_equity(self, equity: float):
        d = self.day
        if d.start_equity <= 0:
            return
        if not d.target_hit and equity >= d.start_equity * (1 + self.cfg.daily_target_pct):
            d.target_hit = True
            d.save()

    @property
    def entries_allowed(self) -> bool:
        return not (self.day.target_hit or self.day.loss_halt)

    @property
    def status(self) -> str:
        d = self.day
        if d.target_hit:
            return f"DAILY TARGET HIT (+{self.cfg.daily_target_pct:.0%}) - no new entries today"
        if d.loss_halt:
            return f"LOSS HALT - {d.halt_reason}"
        return "trading"
