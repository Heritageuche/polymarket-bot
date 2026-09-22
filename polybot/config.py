"""Configuration: config.yaml + .env. Keys are read only from the environment, never logged."""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONTROL_DIR = ROOT / "control"
STATE_DIR = ROOT / "state"
DATA_DIR = ROOT / "data"


@dataclass
class Config:
    bankroll_start: float = 103.60
    daily_target_pct: float = 0.10
    day_timezone: str = "America/New_York"
    assets: List[str] = field(default_factory=lambda: ["btc"])
    intervals: List[str] = field(default_factory=lambda: ["5m", "15m"])
    poll_seconds: float = 2.0
    discovery_seconds: float = 20.0
    warmup_seconds: float = 90.0
    min_edge: float = 0.03
    min_seconds_remaining: int = 40
    max_seconds_remaining: int = 900
    entry_ttl_seconds: int = 25
    prefer_maker: bool = True
    allow_taker: bool = True
    drawdown_level: float = 0.5
    drawdown_prob: float = 0.05
    model_uncertainty_floor: float = 0.05
    max_fraction_per_trade: float = 0.20
    max_total_exposure: float = 0.50
    max_positions: int = 6
    daily_loss_z: float = 2.5
    daily_loss_min_trades: int = 5
    daily_hard_loss_floor: float = 0.08
    daily_hard_loss_cap: float = 0.25
    exit_min_improvement: float = 0.0005
    exit_check_seconds: float = 3.0
    exit_min_hold_seconds: float = 20.0
    exit_conservative_sigmas: float = 1.0
    learn_min_trades: int = 40
    learn_every_trades: int = 20
    learn_every_signals: int = 50
    learn_max_step: float = 0.3
    block_bucket_min_n: int = 25
    # secrets (env only)
    private_key: str = ""
    funder: str = ""
    signature_type: int = 1

    @property
    def has_credentials(self) -> bool:
        return bool(self.private_key and self.funder)


def load_config(path: str | Path | None = None) -> Config:
    load_dotenv(ROOT / ".env")
    path = Path(path) if path else ROOT / "config.yaml"
    raw = {}
    if path.exists():
        raw = yaml.safe_load(path.read_text()) or {}
    known = {k: v for k, v in raw.items() if k in Config.__dataclass_fields__}
    cfg = Config(**known)
    cfg.private_key = os.getenv("POLY_PRIVATE_KEY", "").strip()
    cfg.funder = os.getenv("POLY_FUNDER", "").strip()
    cfg.signature_type = int(os.getenv("POLY_SIGNATURE_TYPE", "1") or 1)
    for d in (CONTROL_DIR, STATE_DIR, DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)
    return cfg
