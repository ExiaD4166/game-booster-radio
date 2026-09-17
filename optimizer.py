"""Local, single-PC game process optimizer.

Raises the target game's priority and CPU affinity, lowers every other process
running on THIS machine, and trims their idle RAM. Everything here is local —
it never touches the network and has no effect on any other user.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass, field

import psutil

if sys.platform != "win32":
    raise RuntimeError("optimizer.py uses Windows-only APIs and can only run on Windows.")

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
            state.original_priorities[pid] = proc.nice()
            proc.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
            trim_working_set(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    logger.info(
        "Boosted '%s' (pid %s) on cores %s; lowered %d background processes.",
        exe_name, game_proc.pid, game_cores, len(state.original_priorities),
    )
    _active_state = state
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
