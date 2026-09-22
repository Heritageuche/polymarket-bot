import polybot.control as control
from polybot.control import RunState
from polybot.risk.daily import DayManager
from polybot.learning.store import Store
from polybot.config import Config
import pathlib, tempfile


def test_run_state_transitions():
    s = RunState(); s.apply("pause"); assert not s.entries_allowed and s.manage_allowed
    s.apply("resume"); assert s.entries_allowed
    s.apply("stop"); assert not s.entries_allowed and s.exit_requested and s.manage_allowed and not s.flatten
    s.apply("resume"); assert not s.entries_allowed  # cannot resume through a stop
    s2 = RunState(); s2.apply("stop-flatten"); assert s2.flatten and s2.manage_allowed
    s3 = RunState(); s3.apply("stop-now"); assert s3.exit_now and not s3.manage_allowed


def test_command_file_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(control, "COMMAND_FILE", tmp_path / "COMMAND")
    monkeypatch.setattr(control, "CONTROL_DIR", tmp_path)
    control.write_command("pause"); assert control.read_command() == "pause"
    control.clear_command(); assert control.read_command() is None


def test_daily_target_and_loss_halt(tmp_path, monkeypatch):
    import polybot.risk.daily as daily
    monkeypatch.setattr(daily, "DAY_FILE", tmp_path / "day.json")
    cfg = Config(); st = Store(tmp_path / "t.db")
    dm = DayManager(cfg, st)
    assert dm.roll_if_needed(103.6)
    assert dm.entries_allowed
    dm.update_equity(110.0); assert not dm.day.target_hit
    dm.update_equity(114.0); assert dm.day.target_hit and not dm.entries_allowed
    dm.update_equity(105.0); assert dm.day.target_hit   # latched: no flip-flopping
    dm2 = DayManager(cfg, Store(tmp_path / "t2.db")); dm2.roll_if_needed(100.0)
    for _ in range(6):
        dm2.on_trade_resolved(pnl=-1.0, ev=0.3, var=0.25)  # each trade 2.6 sigma below EV
    assert dm2.day.loss_halt and "sigma" in dm2.day.halt_reason
