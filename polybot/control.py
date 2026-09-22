"""The OFF switch.

Everything that can stop the bot goes through here so that stopping is *boring*: no strategy
code changes, no model updates, nothing but a state flag the engine checks every loop.

Modes (written to control/COMMAND by the CLI, read by the engine):
  pause         -> no NEW entries; existing positions and open orders keep being managed.
  resume        -> clear pause.
  stop          -> soft stop: no new entries, manage existing positions to resolution, then exit.
  stop-flatten  -> cancel all open orders, exit every position at best available price, then exit.
  stop-now      -> cancel all open orders, exit the process immediately; positions are left as they are
                   (they resolve on their own on Polymarket).
A file control/KILL blocks the bot from starting at all until removed.

Signals: first SIGINT/SIGTERM == stop-now (cancel open orders, exit). Second SIGINT == hard exit.
"""
from __future__ import annotations
import json
import os
import signal
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from .config import CONTROL_DIR, STATE_DIR

COMMAND_FILE = CONTROL_DIR / "COMMAND"
KILL_FILE = CONTROL_DIR / "KILL"
STATUS_FILE = STATE_DIR / "status.json"
PID_FILE = STATE_DIR / "polybot.pid"

VALID = {"pause", "resume", "stop", "stop-flatten", "stop-now"}


def write_command(cmd: str) -> None:
    if cmd not in VALID:
        raise ValueError(f"unknown command {cmd}")
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = COMMAND_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cmd": cmd, "ts": time.time()}))
    os.replace(tmp, COMMAND_FILE)


def read_command() -> str | None:
    if not COMMAND_FILE.exists():
        return None
    try:
        d = json.loads(COMMAND_FILE.read_text())
        return d.get("cmd")
    except Exception:
        return None


def clear_command() -> None:
    try:
        COMMAND_FILE.unlink()
    except FileNotFoundError:
        pass


def kill_file_present() -> bool:
    return KILL_FILE.exists()


def set_kill(on: bool) -> None:
    if on:
        KILL_FILE.write_text(str(time.time()))
    elif KILL_FILE.exists():
        KILL_FILE.unlink()


@dataclass
class RunState:
    """What the engine is allowed to do right now."""
    entries_allowed: bool = True   # may open new positions
    manage_allowed: bool = True    # may manage / exit existing positions
    flatten: bool = False          # actively exit everything
    exit_requested: bool = False   # leave the main loop when safe
    exit_now: bool = False         # leave the main loop immediately
    reason: str = ""

    def apply(self, cmd: str | None) -> None:
        if cmd is None:
            return
        if cmd == "pause":
            self.entries_allowed = False; self.reason = "paused by operator"
        elif cmd == "resume":
            if not self.exit_requested:
                self.entries_allowed = True; self.reason = ""
        elif cmd == "stop":
            self.entries_allowed = False; self.exit_requested = True; self.reason = "soft stop"
        elif cmd == "stop-flatten":
            self.entries_allowed = False; self.flatten = True; self.exit_requested = True; self.reason = "flatten"
        elif cmd == "stop-now":
            self.entries_allowed = False; self.manage_allowed = False; self.exit_now = True
            self.exit_requested = True; self.reason = "stop now"


class SignalHandler:
    def __init__(self, state: RunState):
        self.state = state
        self.count = 0
        signal.signal(signal.SIGINT, self._on)
        signal.signal(signal.SIGTERM, self._on)

    def _on(self, signum, frame):
        self.count += 1
        if self.count >= 2:
            os._exit(130)
        self.state.apply("stop-now")
        self.state.reason = f"signal {signum}"


def write_status(**fields) -> None:
    from . import clock
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fields["heartbeat"] = clock.now()   # exchange time, like every other timestamp in the system
    tmp = STATUS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(fields, indent=1, default=str))
    os.replace(tmp, STATUS_FILE)


def read_status() -> dict:
    if not STATUS_FILE.exists():
        return {}
    try:
        return json.loads(STATUS_FILE.read_text())
    except Exception:
        return {}


def write_pid() -> None:
    PID_FILE.write_text(str(os.getpid()))


def running_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except Exception:
        return None
