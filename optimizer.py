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
import tempfile
import threading
import time
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

# Common heavy background apps and their updaters/helpers — these get pushed
# all the way down to IDLE_PRIORITY_CLASS (what Task Manager labels "Low")
# instead of the default BELOW_NORMAL, since they're frequent, real
# contributors to stutter if left only moderately deprioritized. Matched by
# exact executable name (case-insensitive) — edit this set to add more.
HEAVY_BACKGROUND_NAMES = {
    # Browsers
    "chrome.exe",
    "msedge.exe",
    "msedgewebview2.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
    "opera_gx.exe",
    "vivaldi.exe",
    # Chat / communication
    "discord.exe",
    "discordptt.exe",
    "slack.exe",
    "teams.exe",
    "ms-teams.exe",
    "skype.exe",
    "zoom.exe",
    # Cloud sync clients and app updaters
    "onedrive.exe",
    "dropbox.exe",
    "googledrivesync.exe",
    "googleupdate.exe",
    "microsoftedgeupdate.exe",
    "adobeupdateservice.exe",
    "creativecloud.exe",
    "steamwebhelper.exe",
    "epicgameslauncher.exe",
    "epicwebhelper.exe",
}


def _target_priority_for(name: str) -> int:
    """BELOW_NORMAL by default; IDLE for known-heavy background apps."""
    if name.lower() in HEAVY_BACKGROUND_NAMES:
        return psutil.IDLE_PRIORITY_CLASS
    return psutil.BELOW_NORMAL_PRIORITY_CLASS


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
    known_pids = {leftover.get("game_pid")}
    for pid_str, original_priority in leftover.get("original_priorities", {}).items():
        try:
            pid = int(pid_str)
            known_pids.add(pid)
            psutil.Process(pid).nice(original_priority)
            restored += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            continue

    _reset_unrecorded_stragglers(known_pids)

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

    Known-heavy apps (browsers, chat clients, cloud-sync updaters) drop to
    IDLE_PRIORITY_CLASS; everything else gets the standard BELOW_NORMAL.

    Raises psutil.NoSuchProcess/AccessDenied for the caller to catch — a process
    can exit mid-scan, or belong to another user, at any time.
    """
    pid = proc.pid
    state.original_priorities[pid] = proc.nice()
    proc.nice(_target_priority_for(proc.name()))
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


# A locked file (still held open by a running process) is NOT the only
# thing that makes a temp file "still in use" — plenty of software writes a
# temp file, closes the handle, and reads it back by path later without
# ever holding it open (this very CLI tool's own background-task output
# files work that way, and a first version of this cleanup deleted one out
# from under a live session because of it). File locking alone can't detect
# that, so anything younger than this is left alone regardless — only a
# file nobody has touched in a full day is safe to call actual abandoned
# junk. Matches the same default Windows' own Storage Sense uses.
MIN_TEMP_FILE_AGE_SECONDS = 24 * 60 * 60


def _delete_path(path: Path, cutoff_time: float) -> tuple[int, int]:
    """Best-effort delete of one file, or the old-enough contents of one
    directory tree.

    Returns (files_removed, bytes_freed). Only files last modified before
    `cutoff_time` are touched — anything newer is left alone untouched, on
    the assumption a currently-running app may still care about it (see
    MIN_TEMP_FILE_AGE_SECONDS). A directory is only removed once every file
    in it has either been old enough to delete or wasn't there to begin
    with; one with any recent file left inside simply stays.

    Anything locked by a running process or owned by another account
    (SYSTEM, in C:\\Windows\\Temp) just raises OSError on that one item —
    skipped silently rather than failing the whole cleanup, since that's
    the normal, expected case for a shared temp folder, not a real error.
    """
    removed = 0
    freed = 0

    if path.is_symlink():
        try:
            if path.lstat().st_mtime < cutoff_time:
                path.unlink()
                removed += 1
        except OSError:
            pass
        return removed, freed

    if path.is_dir():
        for root, dirs, files in os.walk(path, topdown=False):
            root_path = Path(root)
            for name in files:
                try:
                    file_path = root_path / name
                    st = file_path.stat()
                    if st.st_mtime >= cutoff_time:
                        continue
                    file_path.unlink()
                    removed += 1
                    freed += st.st_size
                except OSError:
                    pass
            for name in dirs:
                try:
                    (root_path / name).rmdir()  # only succeeds once empty
                except OSError:
                    pass
        try:
            path.rmdir()  # only succeeds once every file above was removed
        except OSError:
            pass
    else:
        try:
            st = path.stat()
            if st.st_mtime < cutoff_time:
                path.unlink()
                removed += 1
                freed += st.st_size
        except OSError:
            pass

    return removed, freed


def clear_temp_folders(min_age_seconds: float = MIN_TEMP_FILE_AGE_SECONDS) -> dict:
    """Best-effort delete of old-enough files inside the temp folders,
    never the folders themselves — Windows expects them to keep existing.

    Two roots: the current user's own temp folder (tempfile.gettempdir(),
    normally AppData\\Local\\Temp — fully owned by this account, so it
    clears completely) and the system-wide C:\\Windows\\Temp, shared with
    services and other accounts. This app runs unelevated, so files there
    it doesn't own simply fail to delete and are skipped — by design, not
    worth an admin-elevation prompt just for a partial extra cleanup.
    """
    cutoff_time = time.time() - min_age_seconds
    roots = [
        Path(tempfile.gettempdir()),
        Path(os.environ.get("SystemRoot", r"C:\Windows")) / "Temp",
    ]

    total_removed = 0
    total_freed = 0
    for root in roots:
        if not root.is_dir():
            continue
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            removed, freed = _delete_path(entry, cutoff_time)
            total_removed += removed
            total_freed += freed

    return {"files_removed": total_removed, "bytes_freed": total_freed}


def _reset_unrecorded_stragglers(known_pids: set[int]) -> None:
    """Catch processes lowered by inheritance, not by us directly.

    A process that spawns *during* a boost inherits its parent's already-
    lowered priority at creation time (Windows' own default behavior for
    child processes) — a new browser tab opened while boosted, for example.
    It never appears in original_priorities, since it didn't exist when we
    scanned, so the loop above has nothing to restore it with. Resetting any
    still-lowered process we have no record of to NORMAL is a safe default:
    that's what a freshly created process would normally start at anyway.
    """
    for proc in psutil.process_iter(["pid"]):
        pid = proc.info["pid"]
        if pid in known_pids or pid in RESERVED_PIDS:
            continue
        try:
            if proc.nice() in (psutil.BELOW_NORMAL_PRIORITY_CLASS, psutil.IDLE_PRIORITY_CLASS):
                proc.nice(psutil.NORMAL_PRIORITY_CLASS)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


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

    _reset_unrecorded_stragglers(set(state.original_priorities) | {state.game_pid})

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
