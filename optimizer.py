"""Local, single-PC game process optimizer.

Raises the target game's priority and lowers the priority of safe-to-lower
background apps on THIS machine (never Windows components, drivers, audio,
input or anti-cheat software), and trims RAM for known-heavy apps. It is a
one-shot operation with no background work afterward. Everything here is
local — it never touches the network and has no effect on any other user.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from ctypes import wintypes
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


# Processes that must keep their normal priority no matter what. Lowering any
# of these makes the game WORSE: the audio engine crackles, the desktop
# compositor (dwm) delays every frame the game presents, graphics-driver
# helpers stall, input tools add latency, capture/overlay tools drop frames,
# and anti-cheat services can flag or kick the player. Windows' own processes
# are also caught by the path rule in _is_protected() (anything running out of
# the Windows folder), this list covers the third-party ones plus a few
# Windows names whose path can't be read without admin rights.
PROTECTED_NAMES = {
    # Windows core / shell / input / audio
    "system", "registry", "memory compression", "smss.exe", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "lsaiso.exe",
    "svchost.exe", "dwm.exe", "fontdrvhost.exe", "explorer.exe", "sihost.exe",
    "taskhostw.exe", "ctfmon.exe", "audiodg.exe", "runtimebroker.exe",
    "shellexperiencehost.exe", "startmenuexperiencehost.exe", "searchhost.exe",
    "textinputhost.exe", "applicationframehost.exe", "conhost.exe",
    "dllhost.exe", "wmiprvse.exe", "spoolsv.exe", "wudfhost.exe", "dashost.exe",
    "lockapp.exe", "securityhealthservice.exe", "smartscreen.exe",
    "msmpeng.exe", "nissrv.exe", "sgrmbroker.exe", "gamebar.exe",
    "gamebarftserver.exe", "gamebarpresencewriter.exe",
    # Graphics drivers and their control panels / overlays
    "nvcontainer.exe", "nvdisplay.container.exe", "nvidia share.exe",
    "nvidia overlay.exe", "nvidia web helper.exe", "nvspcaps64.exe",
    "nvcplui.exe", "nvidia app.exe", "nvsphelper64.exe",
    "radeonsoftware.exe", "amdow.exe", "amdrssrcext.exe", "atieclxx.exe",
    "atiesrxx.exe", "amdfendrsr.exe", "cnext.exe", "amdrsserv.exe",
    "igfxem.exe", "igfxhk.exe", "igfxtray.exe", "igfxcuiservice.exe",
    # Audio drivers / mixers
    "rtkauduservice64.exe", "rtkaudservice64.exe", "rtkngui64.exe",
    "hpaudioswitch.exe", "nahimicservice.exe", "nahimicsvc64.exe",
    "voicemeeter.exe", "voicemeeter8.exe", "voicemeeterpro.exe",
    # Input devices / remapping (lowering these adds input lag)
    "autohotkey.exe", "autohotkey32.exe", "autohotkey64.exe", "etdctrl.exe",
    "lghub.exe", "lghub_agent.exe", "razer synapse 3.exe", "synapse3.exe",
    "steelseriesgg.exe", "steelseriesengine3.exe", "icue.exe",
    # Capture, overlays, hardware monitoring, CPU/GPU tuning
    "obs64.exe", "obs32.exe", "streamlabs obs.exe", "rtss.exe",
    "rtsshooksloader64.exe", "msiafterburner.exe", "hwinfo64.exe",
    "hwinfo32.exe", "cpuz.exe", "coretemp.exe", "throttlestop.exe",
    "ryzenmaster.exe", "gameoverlayui.exe", "eosoverlayrenderer64.exe",
    # Anti-cheat
    "easyanticheat.exe", "easyanticheat_eos.exe", "beservice.exe",
    "bedaisy.exe", "vgc.exe", "vgtray.exe", "faceitclient.exe",
    # Game launcher runtimes a running game commonly depends on
    "steam.exe", "steamservice.exe", "upc.exe", "eadesktop.exe",
    "eabackgroundservice.exe", "battle.net.exe", "rockstarservice.exe",
}


def _is_protected(proc: psutil.Process, game_dir: str | None, keep_pids: set[int]) -> bool:
    """True if this process must be left completely alone.

    Protected: our own/game-related processes (keep_pids: the game, the
    launcher that started it, everything it spawned), anything in
    PROTECTED_NAMES, anything running from the Windows folder (svchost, dwm,
    audiodg, explorer... every built-in component), and anything installed
    in the game's own folder (its anti-cheat, crash handler, helper exes).
    """
    if proc.pid in keep_pids:
        return True
    try:
        name = proc.name()
        if name.lower() in PROTECTED_NAMES:
            return True
        exe = proc.exe()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True  # can't inspect it, so we couldn't safely judge or change it
    exe_lower = exe.lower()
    windir = os.environ.get("SystemRoot", r"C:\Windows").lower()
    if exe_lower.startswith(windir + os.sep):
        return True
    if game_dir and exe_lower.startswith(game_dir + os.sep):
        return True
    return False


def _target_priority_for(name: str) -> int:
    """BELOW_NORMAL by default; IDLE for known-heavy background apps."""
    if name.lower() in HEAVY_BACKGROUND_NAMES:
        return psutil.IDLE_PRIORITY_CLASS
    return psutil.BELOW_NORMAL_PRIORITY_CLASS


@dataclass
class BoostState:
    """What optimize_for_game() changed, so restore_defaults() can undo it.

    Processes are identified by (pid, creation time), never pid alone:
    Windows recycles pids aggressively, so a pid recorded earlier may by the
    time of a restore belong to a completely different program (a driver
    helper, an anti-cheat worker...) that we must not touch.
    """

    game_pid: int
    game_create_time: float | None = None
    game_original_priority: int | None = None
    started_at: float = field(default_factory=time.time)
    original_priorities: dict[int, int] = field(default_factory=dict)
    create_times: dict[int, float] = field(default_factory=dict)
    original_io_priorities: dict[int, int] = field(default_factory=dict)
    original_power_scheme: str | None = None


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
                    "game_create_time": state.game_create_time,
                    "game_original_priority": state.game_original_priority,
                    "started_at": state.started_at,
                    "original_priorities": state.original_priorities,
                    "create_times": state.create_times,
                    "original_io_priorities": state.original_io_priorities,
                    "original_power_scheme": state.original_power_scheme,
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


def _same_process(pid: int, create_time: float | None) -> psutil.Process | None:
    """The process at `pid` — but only if it is still the SAME process.

    A recorded creation time that no longer matches means the pid has since
    been recycled for an unrelated program, so nothing must be done to it.
    `create_time` None (a record from an older version that didn't store it)
    falls back to trusting the pid.
    """
    try:
        proc = psutil.Process(pid)
        if create_time is not None and abs(proc.create_time() - create_time) > 1.0:
            return None
        return proc
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def _restore_recorded(
    priorities: dict, create_times: dict, io_priorities: dict
) -> tuple[set[int], int]:
    """Put every recorded process back how it was. Returns (pids, restored count).

    Dict keys may be ints (in-memory) or strings (read back from JSON).
    """
    known: set[int] = set()
    restored = 0
    times = {int(k): v for k, v in create_times.items()}
    ios = {int(k): v for k, v in io_priorities.items()}
    for pid_key, original in priorities.items():
        try:
            pid = int(pid_key)
        except (TypeError, ValueError):
            continue
        known.add(pid)
        proc = _same_process(pid, times.get(pid))
        if proc is None:
            continue
        try:
            proc.nice(original)
            restored += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if pid in ios:
            try:
                proc.ionice(ios[pid])
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError, ValueError):
                pass
    return known, restored


def recover_from_leftover_state(leftover: dict) -> int:
    """Restore everything recorded in a crash-leftover state file.

    Returns how many background processes were successfully restored.
    """
    game_pid = leftover.get("game_pid")
    game = _same_process(game_pid, leftover.get("game_create_time")) if game_pid else None
    if game is not None:
        try:
            game.nice(leftover.get("game_original_priority") or psutil.NORMAL_PRIORITY_CLASS)
            if "dedicated_cores" in leftover:
                # A record from a version that pinned the game to a few cores.
                game.cpu_affinity(list(range(psutil.cpu_count(logical=True) or 1)))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    known, restored = _restore_recorded(
        leftover.get("original_priorities", {}),
        leftover.get("create_times", {}),
        leftover.get("original_io_priorities", {}),
    )
    known.add(game_pid)
    _reset_unrecorded_stragglers(known, leftover.get("started_at"))
    _restore_power_plan(leftover.get("original_power_scheme"))

    _clear_state_file()
    return restored


def find_processes_by_name(exe_name: str) -> list[psutil.Process]:
    """Every running process whose executable name matches (case-insensitive)."""
    exe_name = exe_name.lower()
    matches = []
    for proc in psutil.process_iter(["pid", "name"]):
        try:
            if proc.info["name"] and proc.info["name"].lower() == exe_name:
                matches.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return matches


def _resident_bytes(proc: psutil.Process) -> int:
    try:
        return proc.memory_info().rss
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return 0


def find_process_by_name(exe_name: str) -> psutil.Process | None:
    """The process to boost for `exe_name`, or None if it isn't running.

    Several processes can share one name — a launcher stub next to the real
    game engine, or a multi-process game. The one using the most memory is
    the real game; a dormant bootstrap stub would otherwise be "boosted"
    while the actual game ran untouched.
    """
    matches = find_processes_by_name(exe_name)
    if not matches:
        return None
    return max(matches, key=_resident_bytes)


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


# Win32 calls used below. Typed prototypes matter on 64-bit Windows: without
# them ctypes treats handles as 32-bit ints and can truncate them.
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_kernel32.OpenProcess.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL
_kernel32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.CreateEventW.restype = wintypes.HANDLE
_kernel32.SetEvent.argtypes = [wintypes.HANDLE]
_kernel32.SetEvent.restype = wintypes.BOOL
_kernel32.WaitForMultipleObjects.argtypes = [
    wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD,
]
_kernel32.WaitForMultipleObjects.restype = wintypes.DWORD
_kernel32.GetCurrentThread.restype = wintypes.HANDLE
_kernel32.SetThreadPriority.argtypes = [wintypes.HANDLE, ctypes.c_int]
_kernel32.SetThreadPriority.restype = wintypes.BOOL

_SYNCHRONIZE = 0x00100000
_INFINITE = 0xFFFFFFFF
_THREAD_MODE_BACKGROUND_BEGIN = 0x00010000
_THREAD_MODE_BACKGROUND_END = 0x00020000
_CREATE_NO_WINDOW = 0x08000000


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (OSError, AttributeError):
        return False


def explain_boost_failure(exe_name: str) -> str:
    """Why optimize_for_game() returned None: 'not_running' or 'needs_admin'.

    A game running as Administrator (many anti-cheat titles do) can't be
    modified by an unelevated program - Windows refuses - which is a very
    different problem from the game simply not being open any more.
    """
    if find_process_by_name(exe_name) is None:
        return "not_running"
    return "needs_admin" if not is_admin() else "unknown"


def _target_io_priority_for(name: str) -> int:
    """Disk I/O tiers mirror the CPU ones: VERYLOW for known-heavy apps, LOW for the rest."""
    if name.lower() in HEAVY_BACKGROUND_NAMES:
        return psutil.IOPRIO_VERYLOW
    return psutil.IOPRIO_LOW


def _lower_io_priority(proc: psutil.Process, name: str, state: BoostState) -> None:
    """Lower a process's disk I/O priority (never raises it), and record the original.

    CPU priority alone doesn't stop a background app from flooding the disk
    queue - a Steam download or browser cache write competes directly with
    the game streaming textures, which shows up as hitches on new areas.
    """
    try:
        original = int(proc.ionice())
        target = _target_io_priority_for(name)
        if original > target:
            proc.ionice(target)
            state.original_io_priorities[proc.pid] = original
    except (psutil.AccessDenied, OSError, ValueError, AttributeError):
        pass


def _deprioritize(proc: psutil.Process, state: BoostState) -> None:
    """Lower a process's CPU and I/O priority, recording what it was before.

    Known-heavy apps (browsers, chat clients, cloud-sync updaters) drop to
    IDLE_PRIORITY_CLASS and have their RAM trimmed; everything else gets the
    standard BELOW_NORMAL and is otherwise left alone. RAM is deliberately NOT
    trimmed for every process: a trimmed process has to page its memory back
    in the moment it next runs, which is itself a source of hitches.

    A process is only recorded once it has actually been lowered, so the
    "N lowered" count is honest; its creation time is stored alongside so a
    later restore can tell it apart from an unrelated process that reuses
    the pid.

    Raises psutil.NoSuchProcess/AccessDenied for the caller to catch - a process
    can exit mid-scan, or belong to another user, at any time.
    """
    pid = proc.pid
    name = proc.name()
    create_time = proc.create_time()
    original = proc.nice()
    proc.nice(_target_priority_for(name))
    state.original_priorities[pid] = int(original)
    state.create_times[pid] = create_time
    _lower_io_priority(proc, name, state)
    if name.lower() in HEAVY_BACKGROUND_NAMES:
        trim_working_set(pid)


# ---- System timer resolution -------------------------------------------------
# Windows' default timer tick is 15.6 ms, so Sleep(1) in a game's loop can
# oversleep by many milliseconds and wobble frame pacing. Asking for the
# finest resolution the hardware supports (normally 0.5 ms) for the length of
# a boost tightens that. It is a plain request, not a hack: Windows applies
# the finest value any program asks for, and releases it when we withdraw it
# (or automatically if this program exits or crashes), so no state needs
# saving. Many games already ask for it themselves, so on such systems the
# gain is small - it never makes anything worse.
_timer_request: int | None = None


def request_fine_timer_resolution() -> bool:
    global _timer_request
    if _timer_request is not None:
        return True
    try:
        ntdll = ctypes.windll.ntdll
        coarsest, finest, current = wintypes.ULONG(), wintypes.ULONG(), wintypes.ULONG()
        if ntdll.NtQueryTimerResolution(
            ctypes.byref(coarsest), ctypes.byref(finest), ctypes.byref(current)
        ) != 0:
            return False
        if ntdll.NtSetTimerResolution(finest.value, True, ctypes.byref(current)) != 0:
            return False
        _timer_request = finest.value
        return True
    except (OSError, AttributeError):
        return False


def release_timer_resolution() -> None:
    global _timer_request
    if _timer_request is None:
        return
    try:
        ctypes.windll.ntdll.NtSetTimerResolution(
            _timer_request, False, ctypes.byref(wintypes.ULONG())
        )
    except (OSError, AttributeError):
        pass
    _timer_request = None


# ---- Power plan ---------------------------------------------------------------
# On the "Balanced" plan the CPU parks cores and drops clocks between calm and
# busy scenes, and ramping back up costs milliseconds - a classic hitch.
# During a boost we switch to the Ultimate/High Performance plan, but ONLY:
#   - if that plan already exists on this PC (we never create or edit plans;
#     many laptops don't offer one at all),
#   - while plugged in (never drain a battery),
#   - and we record the original plan in the crash-recovery file and put it
#     back on Un-boost - unless the user picked a different plan meanwhile.
POWER_PLAN_GUIDS = (
    "e9a42b02-d5df-448d-aa00-03f14749eb61",  # Ultimate Performance
    "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c",  # High Performance
)
_GUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")


def _powercfg(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["powercfg", *args], capture_output=True, text=True, errors="replace",
            timeout=10, creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _active_power_scheme() -> str | None:
    out = _powercfg("/getactivescheme")
    match = _GUID_PATTERN.search(out) if out else None
    return match.group(0).lower() if match else None


def _activate_performance_power_plan() -> str | None:
    """Switch to a performance plan if safe. Returns the ORIGINAL plan's GUID, or None."""
    battery = psutil.sensors_battery()
    if battery is not None and battery.power_plugged is False:
        return None
    active = _active_power_scheme()
    listing = _powercfg("/list")
    if active is None or listing is None or active in POWER_PLAN_GUIDS:
        return None
    available = {g.lower() for g in _GUID_PATTERN.findall(listing)}
    for guid in POWER_PLAN_GUIDS:
        if guid in available:
            return active if _powercfg("/setactive", guid) is not None else None
    return None


def _restore_power_plan(original: str | None) -> None:
    if not original:
        return
    if _active_power_scheme() in POWER_PLAN_GUIDS:
        _powercfg("/setactive", original)


# ---- Game exit detection ------------------------------------------------------
class GameExitWatcher:
    """Signals the moment the boosted game exits - without polling.

    A thread blocks inside the kernel (WaitForMultipleObjects) on the game's
    process handle, using no CPU at all until Windows wakes it. `exited` is a
    threading.Event the GUI can check on its own existing timer.
    """

    def __init__(self, pid: int) -> None:
        self.exited = threading.Event()
        self._pid = pid
        self._thread: threading.Thread | None = None
        self._process_handle = None
        self._stop_handle = None

    def start(self) -> None:
        self._process_handle = _kernel32.OpenProcess(_SYNCHRONIZE, False, self._pid)
        if not self._process_handle:
            # Already gone, or an elevated game we're not allowed to wait on
            # (manual Un-boost still works in that case).
            if not psutil.pid_exists(self._pid):
                self.exited.set()
            return
        self._stop_handle = _kernel32.CreateEventW(None, True, False, None)
        self._thread = threading.Thread(target=self._wait, daemon=True)
        self._thread.start()

    def _wait(self) -> None:
        handles = (wintypes.HANDLE * 2)(self._process_handle, self._stop_handle)
        if _kernel32.WaitForMultipleObjects(2, handles, False, _INFINITE) == 0:
            self.exited.set()

    def stop(self) -> None:
        if self._thread is not None:
            _kernel32.SetEvent(self._stop_handle)
            self._thread.join(timeout=2)
            self._thread = None
        for handle in (self._process_handle, self._stop_handle):
            if handle:
                _kernel32.CloseHandle(handle)
        self._process_handle = self._stop_handle = None


def optimize_for_game(exe_name: str) -> BoostState | None:
    """Boost `exe_name` and lower the priority of safe-to-lower background apps.

    One-shot: everything happens right now and nothing keeps running
    afterward. Returns a BoostState to pass to restore_defaults() later, or
    None if the game isn't running or Windows refused to let us change it
    (see explain_boost_failure()).
    """
    global _active_state
    if _active_state is not None:
        logger.warning(
            "A boost is already active (pid %s); restore it before boosting again.",
            _active_state.game_pid,
        )
        return _active_state

    same_name = find_processes_by_name(exe_name)
    game_proc = find_process_by_name(exe_name)
    if game_proc is None:
        logger.warning("Game process '%s' not found running.", exe_name)
        return None

    # The game is deliberately left free to run on EVERY core. An earlier
    # version pinned it to two logical cores (one physical core with
    # hyperthreading), which measured ~4x slower on a multi-threaded workload
    # and is a direct cause of stutter in any modern game.
    try:
        state = BoostState(
            game_pid=game_proc.pid,
            game_create_time=game_proc.create_time(),
            game_original_priority=int(game_proc.nice()),
        )
        game_proc.nice(psutil.HIGH_PRIORITY_CLASS)
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        logger.warning("Could not boost '%s': %s", exe_name, exc)
        return None

    # Never touch the game's whole family: the launcher that started it,
    # everything it spawned (anti-cheat, crash handler, helpers), and every
    # other process sharing the game's exe name.
    keep_pids = {psutil.Process().pid, game_proc.pid} | RESERVED_PIDS
    keep_pids |= {p.pid for p in same_name}
    try:
        keep_pids |= {p.pid for p in game_proc.parents()}
        keep_pids |= {p.pid for p in game_proc.children(recursive=True)}
        game_dir = str(Path(game_proc.exe()).parent).lower()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        game_dir = None

    for proc in psutil.process_iter(["pid"]):
        try:
            if _is_protected(proc, game_dir, keep_pids):
                continue
            _deprioritize(proc, state)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    state.original_power_scheme = _activate_performance_power_plan()
    request_fine_timer_resolution()

    logger.info(
        "Boosted '%s' (pid %s); lowered %d background processes.",
        exe_name, game_proc.pid, len(state.original_priorities),
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
    # Background mode drops THIS thread's disk-I/O, CPU and memory priority to
    # the lowest tier for the duration (and only this thread - nothing else in
    # the app is affected), so a big cleanup queues behind whatever the game
    # is reading from disk instead of competing with it.
    thread = _kernel32.GetCurrentThread()
    in_background = bool(_kernel32.SetThreadPriority(thread, _THREAD_MODE_BACKGROUND_BEGIN))
    try:
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
    finally:
        if in_background:
            _kernel32.SetThreadPriority(thread, _THREAD_MODE_BACKGROUND_END)

    return {"files_removed": total_removed, "bytes_freed": total_freed}


def _reset_unrecorded_stragglers(known_pids: set[int], started_at: float | None) -> None:
    """Catch processes lowered by inheritance, not by us directly.

    A process that spawns *during* a boost inherits its parent's already-
    lowered priority at creation time (Windows' own default behavior for
    child processes) - a new browser tab opened while boosted, for example.
    It never appears in original_priorities, since it didn't exist when we
    scanned, so the loop in _restore_recorded has nothing to restore it with.

    This must be narrow, though: plenty of programs are DESIGNED to run at
    low priority (search indexer, servicing, updaters) and resetting those
    would hand them more CPU than Windows intended. So only a process that
      - was created after the boost began (it can't have been low beforehand),
      - is not protected (Windows components, drivers, ... see _is_protected),
      - is currently low, and
      - descends from a process we lowered (that's how it inherited the value)
    is reset to normal. A record from an older version has no start time to
    judge by, so it is skipped rather than guessed at.
    """
    if started_at is None:
        return

    candidates: dict[int, int] = {}
    for proc in psutil.process_iter(["pid", "ppid"]):
        pid = proc.info["pid"]
        if pid in known_pids or pid in RESERVED_PIDS:
            continue
        try:
            if proc.create_time() < started_at - 1.0:
                continue
            if proc.nice() not in (psutil.BELOW_NORMAL_PRIORITY_CLASS, psutil.IDLE_PRIORITY_CLASS):
                continue
            if _is_protected(proc, None, set()):
                continue
            candidates[pid] = proc.info["ppid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    def descends_from_lowered(pid: int) -> bool:
        for _ in range(32):
            parent = candidates.get(pid)
            if parent is None:
                return False
            if parent in known_pids:
                return True
            pid = parent
        return False

    for pid in candidates:
        if not descends_from_lowered(pid):
            continue
        try:
            psutil.Process(pid).nice(psutil.NORMAL_PRIORITY_CLASS)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def restore_defaults(state: BoostState) -> None:
    """Undo optimize_for_game(): put everything back exactly as it was."""
    global _active_state
    game = _same_process(state.game_pid, state.game_create_time)
    if game is not None:
        try:
            game.nice(state.game_original_priority or psutil.NORMAL_PRIORITY_CLASS)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    known, _ = _restore_recorded(
        state.original_priorities, state.create_times, state.original_io_priorities
    )
    known.add(state.game_pid)
    _reset_unrecorded_stragglers(known, state.started_at)
    _restore_power_plan(state.original_power_scheme)
    release_timer_resolution()

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

    print(f"Boosted. {len(boost_state.original_priorities)} background processes lowered.")
    input("Press Enter to restore everything to normal...\n")
    restore_defaults(boost_state)
    print("Restored.")
