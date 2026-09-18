"""Desktop GUI shell.

Left pane: game optimizer dashboard, wired to optimizer.py.
Right pane: sync radio controls, wired to sync_client.py and radio_player.py.
"""

from __future__ import annotations

import logging
import queue
import threading
from pathlib import Path

import customtkinter as ctk
import psutil
from PIL import Image, ImageDraw

import optimizer
import radio_player
import sync_client

logger = logging.getLogger(__name__)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

STATS_REFRESH_MS = 1000
QUEUE_POLL_MS = 100
SYNC_POLL_MS = 500
DRIFT_THRESHOLD_SECONDS = 1.5
DEFAULT_SERVER_URI = "ws://localhost:8765"

ASSETS_DIR = Path(__file__).parent / "assets"
ICON_PATH = ASSETS_DIR / "app_icon.ico"

# Visual identity: a Discord-style dark palette instead of CTk's flat default.
FONT = "Segoe UI"
COLOR_BG = "#1E1F22"
COLOR_CARD = "#2B2D31"
COLOR_SURFACE = "#313338"
COLOR_BORDER = "#3F4147"
COLOR_TEXT = "#F2F3F5"
COLOR_TEXT_MUTED = "#949BA4"
COLOR_ACCENT = "#3B3FA0"
COLOR_ACCENT_HOVER = "#2F3280"
COLOR_DANGER = "#A82D2F"
COLOR_DANGER_HOVER = "#8A2426"
COLOR_BUTTON_TEXT = "#D8D9DE"
COLOR_BUSY = "#4E5058"
COLOR_SUCCESS = "#23A559"
COLOR_WARNING = "#F0B232"
COLOR_ACCENT_LIGHT = "#9AA0F5"

# Dark, muted tints for badge backgrounds — each pairs with its matching
# status color above (e.g. COLOR_BADGE_BG_SUCCESS behind COLOR_SUCCESS text).
COLOR_BADGE_BG_NEUTRAL = COLOR_SURFACE
COLOR_BADGE_BG_SUCCESS = "#173A29"
COLOR_BADGE_BG_ACCENT = "#242759"
COLOR_BADGE_BG_WARNING = "#3A2E12"


def _load_bar_color(percent: float) -> str:
    """Green under load, amber under pressure, red when the system is straining."""
    if percent >= 85:
        return COLOR_DANGER
    if percent >= 60:
        return COLOR_WARNING
    return COLOR_SUCCESS


def _make_gamepad_icon(size: int, color: str) -> Image.Image:
    """Draw a flat gamepad silhouette instead of relying on emoji rendering.

    Tk falls back to a crude monochrome outline for the color game-controller
    emoji on this system, so we draw our own icon at 4x and downsample it for
    clean anti-aliased edges instead.
    """
    scale = 4
    s = size * scale
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle(
        [s * 0.04, s * 0.28, s * 0.96, s * 0.72], radius=s * 0.22, fill=color
    )

    # D-pad (left) and face buttons (right), punched out as transparent holes.
    # Kept bold and simple — fine detail disappears once this is downsampled
    # to the ~20px it's actually displayed at.
    cx, cy, arm, thick = s * 0.28, s * 0.50, s * 0.11, s * 0.065
    draw.rectangle([cx - thick, cy - arm, cx + thick, cy + arm], fill=(0, 0, 0, 0))
    draw.rectangle([cx - arm, cy - thick, cx + arm, cy + thick], fill=(0, 0, 0, 0))

    r = s * 0.075
    for bx, by in ((0.68, 0.42), (0.82, 0.58)):
        draw.ellipse([s * bx - r, s * by - r, s * bx + r, s * by + r], fill=(0, 0, 0, 0))

    return img.resize((size, size), Image.LANCZOS)


def _make_app_icon(size: int) -> Image.Image:
    """A solid rounded-square tile with the gamepad mark on top.

    Standalone app icons (unlike the in-window heading icon) conventionally
    sit on a solid colored tile — Discord, Spotify, etc. all do this — so the
    icon stays legible against any taskbar background.
    """
    scale = 4
    s = size * scale
    background = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(background).rounded_rectangle([0, 0, s, s], radius=s * 0.22, fill=COLOR_ACCENT)
    background = background.resize((size, size), Image.LANCZOS)

    mark = _make_gamepad_icon(size // 2, COLOR_TEXT)  # already crisp at its own final size
    offset = ((size - mark.width) // 2, (size - mark.height) // 2)
    background.alpha_composite(mark, offset)
    return background


def _ensure_app_icon() -> Path:
    """Generate the .ico file on first run if it doesn't exist yet, and return its path.

    CustomTkinter resets a titlebar icon set via iconphoto() ~200ms after
    startup (it schedules its own default icon unless iconbitmap() was
    called), so a real .ico file + iconbitmap() is the reliable approach.
    This file also becomes the asset Phase 10's .exe packaging will need.
    """
    if not ICON_PATH.exists():
        ASSETS_DIR.mkdir(parents=True, exist_ok=True)
        _make_app_icon(256).save(
            ICON_PATH, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
        )
    return ICON_PATH


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Game Booster & Radio Center")
        self.geometry("980x620")
        self.minsize(840, 520)
        self.configure(fg_color=COLOR_BG)

        self.iconbitmap(str(_ensure_app_icon()))

        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.stats_monitor = optimizer.StatsMonitor(interval_seconds=1.0)
        self.boost_state: optimizer.BoostState | None = None
        self.boost_maintainer: optimizer.BoostMaintainer | None = None
        self._boost_queue: queue.Queue = queue.Queue()

        self.sync_client: sync_client.SyncClient | None = None
        self.radio_player = radio_player.RadioPlayer(volume=70)
        self.radio_on = True
        self._loaded_track_url: str | None = None
        self._is_synced_playback = False
        self._track_load_queue: queue.Queue = queue.Queue()

        self._build_left_pane()
        self._build_right_pane()
        self._recover_from_previous_crash()

        self.stats_monitor.start()
        self.after(STATS_REFRESH_MS, self._poll_stats)
        self.after(SYNC_POLL_MS, self._poll_sync_state)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _recover_from_previous_crash(self) -> None:
        """Undo a leftover boost from a run that never got to restore_defaults()."""
        leftover = optimizer.find_crash_leftover_state()
        if leftover is None:
            return
        restored = optimizer.recover_from_leftover_state(leftover)
        self.status_label.configure(
            text=f"Recovered from an earlier session — restored {restored} processes.",
            text_color=COLOR_WARNING,
        )

    # ---------------------------------------------------------------- layout

    def _build_left_pane(self) -> None:
        left = ctk.CTkFrame(self, corner_radius=16, fg_color=COLOR_CARD)
        left.grid(row=0, column=0, sticky="nsew", padx=(20, 10), pady=20)
        left.grid_columnconfigure(0, weight=1)

        title_row = ctk.CTkFrame(left, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="w", padx=20, pady=(20, 4))

        self._gamepad_icon = ctk.CTkImage(
            light_image=_make_gamepad_icon(28, COLOR_ACCENT_LIGHT),
            dark_image=_make_gamepad_icon(28, COLOR_ACCENT_LIGHT),
            size=(28, 28),
        )
        ctk.CTkLabel(title_row, text="", image=self._gamepad_icon).grid(
            row=0, column=0, padx=(0, 10)
        )

        ctk.CTkLabel(
            title_row,
            text="GAME",
            font=ctk.CTkFont(family=FONT, size=20, weight="bold"),
            text_color=COLOR_ACCENT_LIGHT,
        ).grid(row=0, column=1)
        ctk.CTkLabel(
            title_row,
            text=" OPTIMIZER",
            font=ctk.CTkFont(family=FONT, size=20, weight="bold"),
            text_color=COLOR_TEXT,
        ).grid(row=0, column=2)

        ctk.CTkLabel(
            left,
            text="Pick a running game, then boost it.",
            font=ctk.CTkFont(family=FONT, size=12),
            text_color=COLOR_TEXT_MUTED,
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 12))

        selector_row = ctk.CTkFrame(left, fg_color="transparent")
        selector_row.grid(row=2, column=0, sticky="ew", padx=20, pady=4)
        selector_row.grid_columnconfigure(0, weight=1)

        self.game_dropdown = ctk.CTkComboBox(
            selector_row,
            values=["(click Refresh)"],
            height=36,
            corner_radius=8,
            font=ctk.CTkFont(family=FONT, size=13),
            fg_color=COLOR_SURFACE,
            border_color=COLOR_BORDER,
            button_color=COLOR_ACCENT,
            button_hover_color=COLOR_ACCENT_HOVER,
            dropdown_fg_color=COLOR_SURFACE,
            dropdown_hover_color=COLOR_ACCENT,
            text_color=COLOR_TEXT,
        )
        self.game_dropdown.grid(row=0, column=0, sticky="ew")
        ctk.CTkButton(
            selector_row,
            text="Refresh",
            width=90,
            height=36,
            corner_radius=8,
            font=ctk.CTkFont(family=FONT, size=13),
            fg_color="transparent",
            hover_color=COLOR_SURFACE,
            border_width=1,
            border_color=COLOR_BORDER,
            text_color=COLOR_TEXT,
            command=self._refresh_process_list,
        ).grid(row=0, column=1, padx=(8, 0))

        self.boost_button = ctk.CTkButton(
            left,
            text="⚡ BOOST GAME",
            height=50,
            corner_radius=10,
            font=ctk.CTkFont(family=FONT, size=17, weight="bold"),
            fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER,
            text_color=COLOR_BUTTON_TEXT,
            command=self._on_boost_clicked,
        )
        self.boost_button.grid(row=3, column=0, sticky="ew", padx=20, pady=20)

        self.status_label = ctk.CTkLabel(
            left,
            text="Idle — no game boosted.",
            font=ctk.CTkFont(family=FONT, size=12),
            text_color=COLOR_TEXT_MUTED,
        )
        self.status_label.grid(row=4, column=0, sticky="w", padx=20)

        self._divider(left).grid(row=5, column=0, sticky="ew", padx=20, pady=(20, 16))

        ctk.CTkLabel(
            left,
            text="SYSTEM LOAD",
            font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
            text_color=COLOR_TEXT_MUTED,
        ).grid(row=6, column=0, sticky="w", padx=20, pady=(0, 8))

        self.cpu_label = ctk.CTkLabel(
            left, text="CPU: --%", font=ctk.CTkFont(family=FONT, size=13), text_color=COLOR_TEXT
        )
        self.cpu_label.grid(row=7, column=0, sticky="w", padx=20)
        self.cpu_bar = ctk.CTkProgressBar(
            left, height=10, corner_radius=6, fg_color=COLOR_SURFACE, progress_color=COLOR_SUCCESS
        )
        self.cpu_bar.set(0)
        self.cpu_bar.grid(row=8, column=0, sticky="ew", padx=20, pady=(6, 14))

        self.ram_label = ctk.CTkLabel(
            left, text="RAM: --%", font=ctk.CTkFont(family=FONT, size=13), text_color=COLOR_TEXT
        )
        self.ram_label.grid(row=9, column=0, sticky="w", padx=20)
        self.ram_bar = ctk.CTkProgressBar(
            left, height=10, corner_radius=6, fg_color=COLOR_SURFACE, progress_color=COLOR_SUCCESS
        )
        self.ram_bar.set(0)
        self.ram_bar.grid(row=10, column=0, sticky="ew", padx=20, pady=(6, 20))

        self._refresh_process_list()

    @staticmethod
    def _divider(parent: ctk.CTkFrame) -> ctk.CTkFrame:
        return ctk.CTkFrame(parent, height=1, fg_color=COLOR_BORDER)

    def _set_sync_badge(self, text: str, text_color: str, bg_color: str) -> None:
        self.sync_badge_label.configure(text=text, text_color=text_color)
        self.sync_badge.configure(fg_color=bg_color)

    def _build_right_pane(self) -> None:
        # A plain frame, matching the left pane's proven layout pattern —
        # CTkScrollableFrame's inner content did not stretch to fill the
        # viewport width here, silently clipping the rightmost widgets with
        # no horizontal scrollbar to reveal them.
        content = ctk.CTkFrame(self, corner_radius=16, fg_color=COLOR_CARD)
        content.grid(row=0, column=1, sticky="nsew", padx=(10, 20), pady=20)
        content.grid_columnconfigure(0, weight=1)

        title_row = ctk.CTkFrame(content, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="w", padx=20, pady=(20, 4))
        ctk.CTkLabel(title_row, text="📻", font=ctk.CTkFont(size=22)).grid(
            row=0, column=0, padx=(0, 8)
        )
        ctk.CTkLabel(
            title_row, text="SYNC", font=ctk.CTkFont(family=FONT, size=20, weight="bold"),
            text_color=COLOR_ACCENT_LIGHT,
        ).grid(row=0, column=1)
        ctk.CTkLabel(
            title_row, text=" RADIO", font=ctk.CTkFont(family=FONT, size=20, weight="bold"),
            text_color=COLOR_TEXT,
        ).grid(row=0, column=2)

        ctk.CTkLabel(
            content, text="Connect to a room to listen with friends.",
            font=ctk.CTkFont(family=FONT, size=12), text_color=COLOR_TEXT_MUTED,
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 12))

        connect_row = ctk.CTkFrame(content, fg_color="transparent")
        connect_row.grid(row=2, column=0, sticky="ew", padx=20, pady=4)
        connect_row.grid_columnconfigure(0, weight=1)
        self.server_entry = ctk.CTkEntry(
            connect_row, placeholder_text=DEFAULT_SERVER_URI,
            height=36, corner_radius=8, font=ctk.CTkFont(family=FONT, size=13),
            fg_color=COLOR_SURFACE, border_color=COLOR_BORDER, text_color=COLOR_TEXT,
        )
        self.server_entry.grid(row=0, column=0, sticky="ew")
        self.connect_button = ctk.CTkButton(
            connect_row, text="Connect", width=100, height=36, corner_radius=8,
            font=ctk.CTkFont(family=FONT, size=13),
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER, text_color=COLOR_BUTTON_TEXT,
            command=self._on_connect_clicked,
        )
        self.connect_button.grid(row=0, column=1, padx=(8, 0))

        self.sync_badge = ctk.CTkFrame(content, corner_radius=12, fg_color=COLOR_BADGE_BG_NEUTRAL)
        self.sync_badge.grid(row=3, column=0, sticky="w", padx=20, pady=(12, 2))
        self.sync_badge_label = ctk.CTkLabel(
            self.sync_badge, text="● DISCONNECTED",
            font=ctk.CTkFont(family=FONT, size=12, weight="bold"), text_color=COLOR_TEXT_MUTED,
        )
        self.sync_badge_label.grid(row=0, column=0, padx=12, pady=4)

        self.track_label = ctk.CTkLabel(
            content, text="No track loaded.", font=ctk.CTkFont(family=FONT, size=13),
            text_color=COLOR_TEXT, wraplength=280, justify="left",
        )
        self.track_label.grid(row=4, column=0, sticky="w", padx=20, pady=(0, 12))

        self._divider(content).grid(row=5, column=0, sticky="ew", padx=20, pady=(0, 12))

        ctk.CTkLabel(
            content, text="LISTENERS", font=ctk.CTkFont(family=FONT, size=12, weight="bold"),
            text_color=COLOR_TEXT_MUTED,
        ).grid(row=6, column=0, sticky="w", padx=20, pady=(0, 6))

        self.users_frame = ctk.CTkFrame(content, fg_color="transparent")
        self.users_frame.grid(row=7, column=0, sticky="ew", padx=20)
        self.users_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self.users_frame, text="Not connected.",
            font=ctk.CTkFont(family=FONT, size=12), text_color=COLOR_TEXT_MUTED,
        ).grid(row=0, column=0, sticky="w")

        self._divider(content).grid(row=8, column=0, sticky="ew", padx=20, pady=(12, 12))

        track_row = ctk.CTkFrame(content, fg_color="transparent")
        track_row.grid(row=9, column=0, sticky="ew", padx=20, pady=4)
        track_row.grid_columnconfigure(0, weight=1)
        self.track_entry = ctk.CTkEntry(
            track_row, placeholder_text="Paste a YouTube link...",
            height=36, corner_radius=8, font=ctk.CTkFont(family=FONT, size=13),
            fg_color=COLOR_SURFACE, border_color=COLOR_BORDER, text_color=COLOR_TEXT,
        )
        self.track_entry.grid(row=0, column=0, sticky="ew")
        self.track_action_button = ctk.CTkButton(
            track_row, text="Play Locally", width=110, height=36, corner_radius=8,
            font=ctk.CTkFont(family=FONT, size=13),
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER, text_color=COLOR_BUTTON_TEXT,
            command=self._on_track_action_clicked,
        )
        self.track_action_button.grid(row=0, column=1, padx=(8, 0))

        playback_row = ctk.CTkFrame(content, fg_color="transparent")
        playback_row.grid(row=10, column=0, sticky="ew", padx=20, pady=(8, 4))
        playback_row.grid_columnconfigure((0, 1), weight=1)
        self.play_pause_button = ctk.CTkButton(
            playback_row, text="▶ Play", height=36, corner_radius=8,
            font=ctk.CTkFont(family=FONT, size=13),
            fg_color="transparent", hover_color=COLOR_SURFACE,
            border_width=1, border_color=COLOR_BORDER, text_color=COLOR_TEXT,
            state="disabled", command=self._on_play_pause_clicked,
        )
        self.play_pause_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.skip_button = ctk.CTkButton(
            playback_row, text="⏭ Skip", height=36, corner_radius=8,
            font=ctk.CTkFont(family=FONT, size=13),
            fg_color="transparent", hover_color=COLOR_SURFACE,
            border_width=1, border_color=COLOR_BORDER, text_color=COLOR_TEXT,
            state="disabled", command=self._on_skip_clicked,
        )
        self.skip_button.grid(row=0, column=1, sticky="ew", padx=(4, 0))

        self._divider(content).grid(row=11, column=0, sticky="ew", padx=20, pady=(16, 12))

        toggle_row = ctk.CTkFrame(content, fg_color="transparent")
        toggle_row.grid(row=12, column=0, sticky="ew", padx=20, pady=(0, 12))
        toggle_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            toggle_row, text="Radio", font=ctk.CTkFont(family=FONT, size=13), text_color=COLOR_TEXT,
        ).grid(row=0, column=0, sticky="w")
        self.radio_switch = ctk.CTkSwitch(
            toggle_row, text="", width=40, progress_color=COLOR_ACCENT,
            command=self._on_radio_toggle,
        )
        self.radio_switch.select()
        self.radio_switch.grid(row=0, column=1, sticky="e")

        volume_row = ctk.CTkFrame(content, fg_color="transparent")
        volume_row.grid(row=13, column=0, sticky="ew", padx=20, pady=(0, 24))
        volume_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            volume_row, text="Volume", font=ctk.CTkFont(family=FONT, size=13), text_color=COLOR_TEXT,
        ).grid(row=0, column=0, sticky="w")
        self.volume_slider = ctk.CTkSlider(
            volume_row, from_=0, to=100, number_of_steps=100,
            progress_color=COLOR_ACCENT, button_color=COLOR_ACCENT_LIGHT,
            button_hover_color=COLOR_ACCENT, command=self._on_volume_change,
        )
        self.volume_slider.set(70)
        self.volume_slider.grid(row=1, column=0, sticky="ew", pady=(4, 0))

    # ------------------------------------------------------------- game list

    def _refresh_process_list(self) -> None:
        names = sorted(
            {
                p.info["name"]
                for p in psutil.process_iter(["name"])
                if p.info["name"] and p.info["name"].lower().endswith(".exe")
            }
        )
        self.game_dropdown.configure(values=names or ["(no processes found)"])
        if names:
            self.game_dropdown.set(names[0])

    # ------------------------------------------------------------ boost flow

    def _on_boost_clicked(self) -> None:
        if self.boost_state is not None:
            self._start_unboost()
        else:
            self._start_boost()

    def _start_boost(self) -> None:
        target = self.game_dropdown.get()
        if not target or target.startswith("("):
            self.status_label.configure(text="Pick a running .exe first.", text_color=COLOR_WARNING)
            return

        self.boost_button.configure(
            state="disabled", text="Boosting...", fg_color=COLOR_BUSY, hover_color=COLOR_BUSY
        )
        self.status_label.configure(text=f"Boosting '{target}'...", text_color=COLOR_TEXT_MUTED)

        def worker() -> None:
            state = optimizer.optimize_for_game(target)
            self._boost_queue.put(("boost_done", target, state))

        threading.Thread(target=worker, daemon=True).start()
        self.after(QUEUE_POLL_MS, self._poll_boost_queue)

    def _start_unboost(self) -> None:
        self.boost_button.configure(
            state="disabled", text="Restoring...", fg_color=COLOR_BUSY, hover_color=COLOR_BUSY
        )

        state = self.boost_state
        maintainer = self.boost_maintainer

        def worker() -> None:
            if maintainer is not None:
                maintainer.stop()
            optimizer.restore_defaults(state)
            self._boost_queue.put(("unboost_done", None, None))

        threading.Thread(target=worker, daemon=True).start()
        self.after(QUEUE_POLL_MS, self._poll_boost_queue)

    def _poll_boost_queue(self) -> None:
        try:
            kind, target, state = self._boost_queue.get_nowait()
        except queue.Empty:
            self.after(QUEUE_POLL_MS, self._poll_boost_queue)
            return

        if kind == "boost_done":
            if state is None:
                self.status_label.configure(
                    text=f"Could not boost '{target}'. Is it still running?", text_color=COLOR_WARNING
                )
                self.boost_button.configure(
                    state="normal", text="⚡ BOOST GAME",
                    fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
                )
            else:
                self.boost_state = state
                self.boost_maintainer = optimizer.BoostMaintainer(state)
                self.boost_maintainer.start()
                self.status_label.configure(
                    text=f"Boosted '{target}' — {len(state.original_priorities)} background processes lowered.",
                    text_color=COLOR_SUCCESS,
                )
                self.boost_button.configure(
                    state="normal", text="⏹ UN-BOOST",
                    fg_color=COLOR_DANGER, hover_color=COLOR_DANGER_HOVER,
                )
        elif kind == "unboost_done":
            self.boost_state = None
            self.boost_maintainer = None
            self.status_label.configure(text="Idle — no game boosted.", text_color=COLOR_TEXT_MUTED)
            self.boost_button.configure(
                state="normal", text="⚡ BOOST GAME",
                fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            )

    # ------------------------------------------------------------- polling

    def _poll_stats(self) -> None:
        stats = self.stats_monitor.get_latest()
        if stats is not None:
            self.cpu_label.configure(text=f"CPU: {stats.cpu_percent:.0f}%")
            self.cpu_bar.set(stats.cpu_percent / 100)
            self.cpu_bar.configure(progress_color=_load_bar_color(stats.cpu_percent))

            self.ram_label.configure(
                text=f"RAM: {stats.ram_percent:.0f}% ({stats.ram_used_gb:.1f} / {stats.ram_total_gb:.1f} GB)"
            )
            self.ram_bar.set(stats.ram_percent / 100)
            self.ram_bar.configure(progress_color=_load_bar_color(stats.ram_percent))
        self.after(STATS_REFRESH_MS, self._poll_stats)

    # --------------------------------------------------------------- sync UI

    def _server_uri(self) -> str:
        return self.server_entry.get().strip() or DEFAULT_SERVER_URI

    def _on_connect_clicked(self) -> None:
        if self.sync_client is not None:
            self._disconnect()
            return
        self.sync_client = sync_client.SyncClient(self._server_uri())
        self.sync_client.start()
        self.connect_button.configure(text="Disconnect")
        self.server_entry.configure(state="disabled")

    def _disconnect(self) -> None:
        if self.sync_client is not None:
            self.sync_client.stop()
            self.sync_client = None
        self.connect_button.configure(text="Connect")
        self.server_entry.configure(state="normal")
        self._set_sync_badge("● DISCONNECTED", COLOR_TEXT_MUTED, COLOR_BADGE_BG_NEUTRAL)
        self._update_users_list([])
        self.track_action_button.configure(state="normal")
        self.play_pause_button.configure(state="disabled")
        self.skip_button.configure(state="disabled")
        if self._is_synced_playback:
            self.radio_player.stop()
            self._is_synced_playback = False
            self._loaded_track_url = None
            self.track_label.configure(text="No track loaded.")

    def _poll_sync_state(self) -> None:
        if self.sync_client is not None:
            connected = self.sync_client.is_connected()
            state = self.sync_client.get_latest_state()
            if connected and state is not None:
                self._apply_sync_state(state)
            elif not connected:
                self._set_sync_badge("● RECONNECTING...", COLOR_WARNING, COLOR_BADGE_BG_WARNING)
        self.after(SYNC_POLL_MS, self._poll_sync_state)

    def _apply_sync_state(self, state: dict) -> None:
        is_admin = state.get("your_role") == "admin"
        sync_active = state.get("sync_active", False)
        track_url = state.get("track_url")
        server_is_playing = state.get("is_playing", False)
        server_position = state.get("position", 0.0)

        self._update_users_list(state.get("users", []))

        if sync_active:
            self._set_sync_badge("🔒 SYNCED TO ADMIN STREAM", COLOR_SUCCESS, COLOR_BADGE_BG_SUCCESS)

            if track_url and (not self._is_synced_playback or track_url != self._loaded_track_url):
                self._is_synced_playback = True
                self._loaded_track_url = track_url
                self._start_load_track(
                    track_url, seek_to=server_position, autoplay=server_is_playing and self.radio_on
                )
            elif self._is_synced_playback and self.radio_on:
                if server_is_playing and not self.radio_player.is_playing():
                    self.radio_player.play()
                elif not server_is_playing and self.radio_player.is_playing():
                    self.radio_player.pause()
                self._correct_drift(server_position)

            self.track_entry.configure(
                state="normal" if is_admin else "disabled",
                placeholder_text="Paste a YouTube link..." if is_admin else "An admin stream is live",
            )
            self.track_action_button.configure(text="Sync for Everyone", state="normal" if is_admin else "disabled")
            self.play_pause_button.configure(state="normal" if is_admin else "disabled")
            self.skip_button.configure(state="normal" if is_admin else "disabled")
        else:
            if self._is_synced_playback:
                self.radio_player.stop()
                self._is_synced_playback = False
                self._loaded_track_url = None
                self.track_label.configure(text="No track loaded.")

            self._set_sync_badge("🎧 LOCAL PLAYER MODE", COLOR_ACCENT_LIGHT, COLOR_BADGE_BG_ACCENT)
            self.track_entry.configure(state="normal", placeholder_text="Paste a YouTube link...")
            self.track_action_button.configure(
                text="Sync for Everyone" if is_admin else "Play Locally", state="normal"
            )
            self.play_pause_button.configure(state="normal" if self._loaded_track_url else "disabled")
            self.skip_button.configure(state="disabled")

    def _correct_drift(self, server_position: float) -> None:
        local_position = self.radio_player.get_position_seconds()
        if abs(local_position - server_position) > DRIFT_THRESHOLD_SECONDS:
            self.radio_player.seek(server_position)

    def _start_load_track(self, url: str, seek_to: float = 0.0, autoplay: bool = True) -> None:
        self.track_label.configure(text=f"Loading '{url}'...")

        def worker() -> None:
            try:
                info = self.radio_player.load(url)
                self._track_load_queue.put(("load_done", info, seek_to, autoplay, None))
            except radio_player.ExtractionError as exc:
                self._track_load_queue.put(("load_failed", None, None, None, str(exc)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(QUEUE_POLL_MS, self._poll_track_load_queue)

    def _poll_track_load_queue(self) -> None:
        try:
            kind, info, seek_to, autoplay, error = self._track_load_queue.get_nowait()
        except queue.Empty:
            self.after(QUEUE_POLL_MS, self._poll_track_load_queue)
            return

        if kind == "load_done":
            self.track_label.configure(text=f"Now playing: {info['title']}")
            if seek_to:
                self.radio_player.seek(seek_to)
            if autoplay:
                self.radio_player.play()
            # Only the icon changes here — whether the button is ENABLED is
            # decided solely by _apply_sync_state's role check, so a track
            # finishing load can never itself re-enable a locked-out control.
            self.play_pause_button.configure(text="⏸ Pause" if autoplay else "▶ Play")
        elif kind == "load_failed":
            logger.warning("Track load failed: %s", error)
            self.track_label.configure(
                text="Could not load that link — it may be unavailable, private, or region-locked."
            )
            self._is_synced_playback = False
            self._loaded_track_url = None

    def _on_track_action_clicked(self) -> None:
        url = self.track_entry.get().strip()
        if not url:
            self.track_label.configure(text="Paste a YouTube link first.")
            return

        state = self.sync_client.get_latest_state() if self.sync_client else None
        is_admin = bool(state and state.get("your_role") == "admin")
        sync_active = bool(state and state.get("sync_active"))

        if is_admin:
            self.sync_client.send({"type": "set_playlist", "urls": [url]})
            self.track_entry.delete(0, "end")
        elif not sync_active:
            self._is_synced_playback = False
            self._loaded_track_url = url
            self._start_load_track(url, seek_to=0.0, autoplay=True)
            self.track_entry.delete(0, "end")

    def _on_play_pause_clicked(self) -> None:
        state = self.sync_client.get_latest_state() if self.sync_client else None
        if state and state.get("your_role") == "admin" and state.get("sync_active"):
            self.sync_client.send({"type": "pause" if state.get("is_playing") else "play"})
        else:
            if self.radio_player.is_playing():
                self.radio_player.pause()
                self.play_pause_button.configure(text="▶ Play")
            else:
                self.radio_player.resume()
                self.play_pause_button.configure(text="⏸ Pause")

    def _on_skip_clicked(self) -> None:
        if self.sync_client is not None:
            self.sync_client.send({"type": "skip"})

    def _on_promote_clicked(self, user_id: int) -> None:
        if self.sync_client is not None:
            self.sync_client.send({"type": "promote", "target_id": user_id})

    def _update_users_list(self, users: list[dict]) -> None:
        for widget in self.users_frame.winfo_children():
            widget.destroy()

        if not users:
            ctk.CTkLabel(
                self.users_frame, text="Not connected.",
                font=ctk.CTkFont(family=FONT, size=12), text_color=COLOR_TEXT_MUTED,
            ).grid(row=0, column=0, sticky="w")
            return

        my_state = self.sync_client.get_latest_state() if self.sync_client else None
        my_id = my_state.get("your_id") if my_state else None
        i_am_admin = bool(my_state and my_state.get("your_role") == "admin")

        for row, user in enumerate(users):
            row_frame = ctk.CTkFrame(self.users_frame, fg_color="transparent")
            row_frame.grid(row=row, column=0, sticky="ew", pady=2)
            row_frame.grid_columnconfigure(0, weight=1)

            label_text = user["name"]
            if user["id"] == my_id:
                label_text += " (you)"
            badge = "👑 admin" if user["role"] == "admin" else "listener"

            ctk.CTkLabel(
                row_frame, text=f"{label_text} — {badge}",
                font=ctk.CTkFont(family=FONT, size=12), text_color=COLOR_TEXT,
            ).grid(row=0, column=0, sticky="w")

            if i_am_admin and user["role"] != "admin":
                ctk.CTkButton(
                    row_frame, text="Promote", width=70, height=24, corner_radius=6,
                    font=ctk.CTkFont(family=FONT, size=11),
                    fg_color="transparent", hover_color=COLOR_SURFACE,
                    border_width=1, border_color=COLOR_BORDER, text_color=COLOR_TEXT,
                    command=lambda uid=user["id"]: self._on_promote_clicked(uid),
                ).grid(row=0, column=1, padx=(8, 0))

    def _on_radio_toggle(self) -> None:
        self.radio_on = bool(self.radio_switch.get())
        if self.radio_on:
            if self._loaded_track_url is not None:
                self.radio_player.resume()
                if self._is_synced_playback and self.sync_client is not None:
                    state = self.sync_client.get_latest_state()
                    if state is not None:
                        self._correct_drift(state.get("position", 0.0))
        else:
            self.radio_player.pause()

    def _on_volume_change(self, value: float) -> None:
        self.radio_player.set_volume(int(value))

    # ---------------------------------------------------------------- close

    def _on_close(self) -> None:
        self.stats_monitor.stop()
        if self.boost_maintainer is not None:
            self.boost_maintainer.stop()
        if self.boost_state is not None:
            optimizer.restore_defaults(self.boost_state)
        if self.sync_client is not None:
            self.sync_client.stop()
        self.radio_player.release()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
