"""Local, single-PC game process optimizer.

Raises the target game's priority and CPU affinity, lowers every other process
running on THIS machine, and trims their idle RAM. Everything here is local —
it never touches the network and has no effect on any other user.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import psutil

if sys.platform != "win32":
    raise RuntimeError("optimizer.py uses Windows-only APIs and can only run on Windows.")

# Where the crash-recovery record lives: outside the project folder, since a
# packaged .exe may run from a read-only install location.
STATE_DIR = Path(os.environ.get("LOCALAPPDATA", os.path.expanduser("~"))) / "GameBoosterRadio"
STATE_FILE = STATE_DIR / "boost_state.json"

logger = logging.getLogger(__name__)

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_SET_QUOTA = 0x0100

# Fixed, well-known PIDs Windows reserves for itself — never touch these.
RESERVED_PIDS = {0, 4}


@dataclass
class BoostState:
    """What optimize_for_game() changed, so restore_defaults() can undo it."""

    game_pid: int
    dedicated_cores: list[int] = field(default_factory=list)
    original_priorities: dict[int, int] = field(default_factory=dict)


# Tracks the one boost currently in effect, so a second call can't overwrite
# original_priorities with values that are already lowered.
_active_state: BoostState | None = None


def _save_state_file(state: BoostState) -> None:
    """Persist the boost record to disk so a crash doesn't lose it.

    Written after lowering processes so a crash any time after that point —
    including one this program can't catch — leaves a record the next launch
    can find and undo. It won't protect against a crash mid-scan, but that
    window is a fraction of a second; this covers the much longer time the
    boost actually stays active.
    """
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(
            json.dumps(
                {
                    "game_pid": state.game_pid,
                    "dedicated_cores": state.dedicated_cores,
                    "original_priorities": state.original_priorities,
                }
            )
        )
    except OSError as exc:
        logger.warning("Could not save boost state file: %s", exc)


def _clear_state_file() -> None:
    try:
        STATE_FILE.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not remove boost state file: %s", exc)


def find_crash_leftover_state() -> dict | None:
    """Check for a boost record a previous, crashed run left behind.

    Call this once at app startup, before doing anything else. Returns the
    raw record from disk, or None if there's nothing to recover.
    """
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read leftover boost state file: %s", exc)
        return None


def recover_from_leftover_state(leftover: dict) -> int:
    """Restore priorities recorded in a crash-leftover state file.

    Returns how many background processes were successfully restored.
    """
    try:
        game_proc = psutil.Process(leftover["game_pid"])
        game_proc.nice(psutil.NORMAL_PRIORITY_CLASS)
        game_proc.cpu_affinity(list(range(psutil.cpu_count(logical=True) or 1)))
    except (psutil.NoSuchProcess, psutil.AccessDenied, KeyError):
        pass

    restored = 0
    for pid_str, original_priority in leftover.get("original_priorities", {}).items():
        try:
            psutil.Process(int(pid_str)).nice(original_priority)
            restored += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            continue

    _clear_state_file()
    return restored


def find_process_by_name(exe_name: str) -> psutil.Process | None:
    """Return the first running process whose executable name matches (case-insensitive)."""
    exe_name = exe_name.lower()
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == exe_name:
                return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return None


def trim_working_set(pid: int) -> bool:
    """Ask Windows to release a process's idle RAM pages back to the system."""
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_INFORMATION | PROCESS_SET_QUOTA, False, pid
    )
    if not handle:
        return False
    try:
        return bool(ctypes.windll.psapi.EmptyWorkingSet(handle))
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _deprioritize(proc: psutil.Process, state: BoostState) -> None:
    """Record a process's current priority, lower it, and trim its RAM.

    Raises psutil.NoSuchProcess/AccessDenied for the caller to catch — a process
    can exit mid-scan, or belong to another user, at any time.
    """
    pid = proc.pid
    state.original_priorities[pid] = proc.nice()
    proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    trim_working_set(pid)


def optimize_for_game(exe_name: str, core_count: int = 2) -> BoostState | None:
    """Boost `exe_name` and deprioritize everything else on this PC.

    Returns a BoostState to pass to restore_defaults() later, or None if the
    game isn't currently running.
    """
    global _active_state
    if _active_state is not None:
        logger.warning(
            "A boost is already active (pid %s); restore it before boosting again.",
            _active_state.game_pid,
        )
        return _active_state

    game_proc = find_process_by_name(exe_name)
    if game_proc is None:
        logger.warning("Game process '%s' not found running.", exe_name)
        return None

    total_cores = psutil.cpu_count(logical=True) or 1
    core_count = min(core_count, total_cores)
    # Core 0 fields a disproportionate share of Windows' own interrupts, so we
    # dedicate the highest-numbered cores to the game rather than the first ones.
    game_cores = list(range(total_cores - core_count, total_cores))

    state = BoostState(game_pid=game_proc.pid, dedicated_cores=game_cores)

    try:
        game_proc.nice(psutil.HIGH_PRIORITY_CLASS)
        game_proc.cpu_affinity(game_cores)
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        logger.warning("Could not boost '%s': %s", exe_name, exc)
        return None

    own_pid = psutil.Process().pid
    for proc in psutil.process_iter(["pid", "name"]):
        pid = proc.info["pid"]
        if pid in RESERVED_PIDS or pid == game_proc.pid or pid == own_pid:
            continue

        try:
            _deprioritize(proc, state)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    logger.info(
        "Boosted '%s' (pid %s) on cores %s; lowered %d background processes.",
        exe_name, game_proc.pid, game_cores, len(state.original_priorities),
    )
    _active_state = state
    _save_state_file(state)
    return state


def restore_defaults(state: BoostState) -> None:
    """Undo optimize_for_game(): restore the game and every lowered process."""
    global _active_state
    try:
        game_proc = psutil.Process(state.game_pid)
        game_proc.nice(psutil.NORMAL_PRIORITY_CLASS)
        game_proc.cpu_affinity(list(range(psutil.cpu_count(logical=True) or 1)))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    for pid, original_priority in state.original_priorities.items():
        try:
            psutil.Process(pid).nice(original_priority)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    if _active_state is state:
        _active_state = None
    _clear_state_file()


@dataclass
class SystemStats:
    """One snapshot of system-wide resource usage."""

    cpu_percent: float
    ram_percent: float
    ram_used_gb: float
    ram_total_gb: float


def get_system_stats() -> SystemStats:
    """Take a single, cheap snapshot of system-wide CPU and RAM usage."""
    ram = psutil.virtual_memory()
    return SystemStats(
        cpu_percent=psutil.cpu_percent(interval=None),
        ram_percent=ram.percent,
        ram_used_gb=ram.used / (1024 ** 3),
        ram_total_gb=ram.total / (1024 ** 3),
    )


class StatsMonitor:
    """Polls system CPU/RAM on a background thread; callers read get_latest()."""

    def __init__(self, interval_seconds: float = 1.0):
        self.interval_seconds = interval_seconds
        self._latest: SystemStats | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        psutil.cpu_percent(interval=None)  # prime the baseline; first real reading follows
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def get_latest(self) -> SystemStats | None:
        with self._lock:
            return self._latest

    def _run(self) -> None:
        while not self._stop_event.is_set():
            stats = get_system_stats()
            with self._lock:
                self._latest = stats
            self._stop_event.wait(self.interval_seconds)


class BoostMaintainer:
    """While a boost is active, periodically lowers newly-spawned processes too.

    optimize_for_game() only sees processes that exist at the moment it's called.
    A browser opening a new tab five minutes later would spawn at normal
    priority otherwise — this catches those on a slower, cheaper-average cadence.
    """

    def __init__(self, state: BoostState, interval_seconds: float = 5.0):
        self.state = state
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._sweep_new_processes()
            self._stop_event.wait(self.interval_seconds)

    def _sweep_new_processes(self) -> None:
        own_pid = psutil.Process().pid
        caught_any = False
        for proc in psutil.process_iter(["pid", "name"]):
            pid = proc.info["pid"]
            if (
                pid in RESERVED_PIDS
                or pid == self.state.game_pid
                or pid == own_pid
                or pid in self.state.original_priorities
            ):
                continue
            try:
                _deprioritize(proc, self.state)
                caught_any = True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if caught_any:
            _save_state_file(self.state)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if len(sys.argv) != 2:
        print("Usage: python optimizer.py <game_exe_name>")
        print("Example (no game handy? test with): python optimizer.py notepad.exe")
        raise SystemExit(1)

    target = sys.argv[1]
    print(f"Boosting '{target}'...")
    boost_state = optimize_for_game(target)

    if boost_state is None:
        print(f"Could not find or boost '{target}'. Is it running?")
        raise SystemExit(1)

    print(f"Boosted on cores {boost_state.dedicated_cores}. "
          f"{len(boost_state.original_priorities)} background processes lowered.")
    input("Press Enter to restore everything to normal...\n")
    restore_defaults(boost_state)
    print("Restored.")
