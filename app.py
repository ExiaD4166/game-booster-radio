"""Desktop GUI shell.

Left pane: game optimizer dashboard, wired to optimizer.py.
Right pane: placeholder for the sync radio controls, built in a later phase.
"""

from __future__ import annotations

import queue
import threading

import customtkinter as ctk
import psutil
from PIL import Image, ImageDraw

import optimizer

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

STATS_REFRESH_MS = 1000
QUEUE_POLL_MS = 100

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


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Game Booster & Radio Center")
        self.geometry("980x620")
        self.minsize(840, 520)
        self.configure(fg_color=COLOR_BG)

        self.grid_columnconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self.stats_monitor = optimizer.StatsMonitor(interval_seconds=1.0)
        self.boost_state: optimizer.BoostState | None = None
        self.boost_maintainer: optimizer.BoostMaintainer | None = None
        self._boost_queue: queue.Queue = queue.Queue()

        self._build_left_pane()
        self._build_right_pane_placeholder()

        self.stats_monitor.start()
        self.after(STATS_REFRESH_MS, self._poll_stats)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

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

    def _build_right_pane_placeholder(self) -> None:
        right = ctk.CTkFrame(self, corner_radius=16, fg_color=COLOR_CARD)
        right.grid(row=0, column=1, sticky="nsew", padx=(10, 20), pady=20)
        right.grid_rowconfigure(0, weight=1)
        right.grid_columnconfigure(0, weight=1)

        placeholder = ctk.CTkFrame(right, fg_color="transparent")
        placeholder.grid(row=0, column=0)
        ctk.CTkLabel(
            placeholder, text="📻", font=ctk.CTkFont(family=FONT, size=40)
        ).pack(pady=(0, 8))
        ctk.CTkLabel(
            placeholder,
            text="Radio Control Center\ncoming in a later phase",
            font=ctk.CTkFont(family=FONT, size=15),
            text_color=COLOR_TEXT_MUTED,
            justify="center",
        ).pack()

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

    # ---------------------------------------------------------------- close

    def _on_close(self) -> None:
        self.stats_monitor.stop()
        if self.boost_maintainer is not None:
            self.boost_maintainer.stop()
        if self.boost_state is not None:
            optimizer.restore_defaults(self.boost_state)
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
