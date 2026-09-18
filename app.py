"""Desktop GUI shell.

Left pane: game optimizer dashboard, wired to optimizer.py.
Right pane: sync radio controls, wired to sync_client.py and radio_player.py.
"""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
import tkinter as tk
from pathlib import Path

import customtkinter as ctk
import psutil
from PIL import Image, ImageDraw, ImageFilter

import optimizer
import radio_player
import sync_client

logger = logging.getLogger(__name__)

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

STATS_REFRESH_MS = 1000
QUEUE_POLL_MS = 100
SYNC_POLL_MS = 500
STATS_HIDDEN_REFRESH_MS = 5000
# Drift correction, gentlest tool first. A real network stream buffers now
# and then (position stalls while the server's clock keeps ticking) and a
# seek on it forces a re-buffer that is audible as a hitch - and back-to-back
# seeks can overshoot each other, which once sounded like constant skipping.
# So small drift is closed by playing a few percent fast or slow (VLC
# time-stretches, so pitch is unchanged) and a hard seek is kept for large
# gaps, with a cooldown so one seek can settle before another may fire.
# Hysteresis (start at 1.0s, stop at 0.4s) stops it flapping around the edge.
DRIFT_NUDGE_START_SECONDS = 1.0
DRIFT_NUDGE_STOP_SECONDS = 0.4
DRIFT_NUDGE_RATE = 0.05
# Right after joining, one seek is fine (playback just started, so there is
# nothing to interrupt) - this is what puts a mid-track joiner in the right place.
DRIFT_JOIN_SEEK_THRESHOLD_SECONDS = 1.0
DRIFT_SEEK_THRESHOLD_SECONDS = 6.0
DRIFT_SEEK_COOLDOWN_SECONDS = 6.0
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
COLOR_CARD_BORDER = "#383A40"
SHADOW_MARGIN = 12

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


def _format_bytes(n: int) -> str:
    """Human-readable size, e.g. 512 MB or 1.3 GB — or 340 KB for anything
    under 1 MB, so a small temp-file cleanup doesn't misleadingly round
    down to a flat "0 MB"."""
    gb = n / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    mb = n / (1024 ** 2)
    if mb >= 1:
        return f"{mb:.0f} MB"
    return f"{n / 1024:.0f} KB"


def _server_position_now(state: dict) -> float:
    """Where the shared stream is RIGHT NOW, not when the state was sent.

    The server broadcasts its position every few seconds, so a state message
    can be seconds old by the time it's used. Comparing that stale number to
    the live local position made a perfectly in-sync listener look up to
    3 seconds behind. While the stream is playing the position advances in
    real time, so add the time since the message arrived.
    """
    position = state.get("position", 0.0)
    received_at = state.get("_received_at")
    if state.get("is_playing") and received_at is not None:
        position += time.monotonic() - received_at
    return position


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _lerp_color(a: str, b: str, t: float) -> str:
    """Blend two #rrggbb colors; t=0 gives a, t=1 gives b."""
    (r1, g1, b1), (r2, g2, b2) = _hex_to_rgb(a), _hex_to_rgb(b)
    mix = lambda x, y: max(0, min(255, round(x + (y - x) * t)))
    return f"#{mix(r1, r2):02x}{mix(g1, g2):02x}{mix(b1, b2):02x}"


def _ease_out(t: float) -> float:
    return 1 - (1 - t) ** 3


def _make_shadow_image(width: int, height: int, margin: float, radius: float,
                       blur: float, dy: float, opacity: float, bg: str,
                       glow: str = COLOR_ACCENT, glow_opacity: float = 0.7) -> Image.Image:
    """A soft glow plus drop shadow of a rounded rectangle, already blended onto `bg`.

    Tk has no real transparency, so a shadow can't be layered over other
    widgets; instead the (solid-colored) background behind a card is drawn as
    an image that already contains it. On a background this dark a pure black
    shadow barely shows, so a faint accent-colored glow (lighter than the
    background) sits under a tighter dark shadow - together they read as depth.
    The blur is done on a quarter-size mask and scaled up - the result is
    blurry anyway, and this keeps it fast even for a large window.
    """
    k = 4
    mw, mh = max(width // k, 1), max(height // k, 1)

    def layer(offset_y: float, sigma: float, alpha: float, inset: float = 0) -> Image.Image:
        mask = Image.new("L", (mw, mh), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [(margin + inset) / k, (margin + offset_y) / k,
             (width - margin - inset) / k, (height - margin + offset_y) / k],
            radius=radius / k, fill=int(255 * alpha),
        )
        return mask.filter(ImageFilter.GaussianBlur(sigma / k)).resize((width, height), Image.BILINEAR)

    image = Image.new("RGB", (width, height), bg)
    image.paste(Image.new("RGB", (width, height), _hex_to_rgb(glow)), (0, 0), layer(0, blur, glow_opacity))
    # The dark shadow is inset from the sides and pushed down, so it only shows
    # beneath the card and doesn't cancel the glow along the edges.
    image.paste(Image.new("RGB", (width, height), (0, 0, 0)), (0, 0), layer(dy, blur * 0.7, opacity, inset=blur * 0.8))
    return image


def _make_gradient_bar(width: int, height: int, left: str, right: str) -> Image.Image:
    """A thin horizontal gradient, used as an accent underline beneath titles."""
    bar = Image.new("RGB", (width, height))
    px = bar.load()
    for x in range(width):
        color = _hex_to_rgb(_lerp_color(left, right, x / max(width - 1, 1)))
        for y in range(height):
            px[x, y] = color
    return bar


class FadeButton(ctk.CTkButton):
    """A CTkButton whose hover color eases in and out instead of snapping.

    CTkButton's own hover is instantaneous, which reads as stiff. This turns
    that off and cross-fades the fill itself. It keeps track of the color the
    app last asked for, so code that recolors a button (Boost turning red, a
    button greying out) still works while the pointer is over it.
    """

    FADE_SECONDS = 0.14

    def __init__(self, master, *args, parent_bg: str = COLOR_CARD, **kwargs):
        self._parent_bg = parent_bg
        self._base = kwargs.get("fg_color", COLOR_ACCENT)
        self._shown = None
        self._hovering = False
        self._fade_job = None
        self._leave_job = None
        super().__init__(master, *args, hover=False, **kwargs)
        self._shown = self._resolve(self._base)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)

    def _resolve(self, color) -> str:
        if isinstance(color, (tuple, list)):
            color = color[-1]
        return (self._parent_bg if color == "transparent" else color).lower()

    def _can_animate(self) -> bool:
        try:
            return self.winfo_toplevel().state() == "normal"
        except tk.TclError:
            return False

    def _apply(self, color) -> None:
        self._shown = self._resolve(color)
        ctk.CTkButton.configure(self, fg_color=color)

    def _cancel_fade(self) -> None:
        if self._fade_job is not None:
            self.after_cancel(self._fade_job)
            self._fade_job = None

    def _fade(self, target: str, final=None) -> None:
        self._cancel_fade()
        start = self._shown or self._resolve(self._base)
        if start == target or not self._can_animate():
            self._apply(final if final is not None else target)
            return
        t0 = time.monotonic()

        def step() -> None:
            progress = min((time.monotonic() - t0) / self.FADE_SECONDS, 1.0)
            self._apply(_lerp_color(start, target, _ease_out(progress)))
            if progress < 1.0:
                self._fade_job = self.after(16, step)
            else:
                self._fade_job = None
                if final is not None:
                    self._apply(final)

        step()

    def _on_enter(self, _event=None) -> None:
        if self._leave_job is not None:
            self.after_cancel(self._leave_job)
            self._leave_job = None
        if self.cget("state") == "disabled":
            return
        self._hovering = True
        self._fade(self._resolve(self.cget("hover_color")))

    def _on_leave(self, _event=None) -> None:
        # Moving between the button's inner widgets fires Leave immediately
        # followed by Enter; the short delay stops that flickering.
        self._leave_job = self.after(35, self._finish_leave)

    def _finish_leave(self) -> None:
        self._leave_job = None
        if self._hovering:
            self._hovering = False
            self._fade(self._resolve(self._base), final=self._base)

    def configure(self, require_redraw=False, **kwargs):
        if "fg_color" in kwargs:
            self._base = kwargs["fg_color"]
            self._cancel_fade()
            disabling = kwargs.get("state") == "disabled"
            if self._hovering and not disabling and self.cget("state") != "disabled":
                kwargs["fg_color"] = kwargs.get("hover_color") or self.cget("hover_color")
            self._shown = self._resolve(kwargs["fg_color"])
        elif kwargs.get("state") == "disabled" and self._hovering:
            self._hovering = False
            self._cancel_fade()
            kwargs["fg_color"] = self._base
            self._shown = self._resolve(self._base)
        super().configure(require_redraw=require_redraw, **kwargs)


class ShadowFrame(ctk.CTkFrame):
    """Container that draws a soft drop shadow behind the card placed inside it.

    Create the card as a child of this frame and grid it with
    padx/pady=SHADOW_MARGIN. The shadow image is regenerated (debounced) when
    the frame is resized, and cached per size.
    """

    def __init__(self, master, radius: int = 16):
        super().__init__(master, fg_color=COLOR_BG, corner_radius=0)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._radius = radius
        self._size = None
        self._job = None
        self._image = None
        self._shadow = ctk.CTkLabel(self, text="", fg_color=COLOR_BG)
        self._shadow.place(x=0, y=0, relwidth=1, relheight=1)
        self.bind("<Configure>", self._schedule_render)

    def _schedule_render(self, _event=None) -> None:
        if self._job is not None:
            self.after_cancel(self._job)
        self._job = self.after(60, self._render)

    def _render(self) -> None:
        self._job = None
        width, height = self.winfo_width(), self.winfo_height()
        if width < 60 or height < 60 or (width, height) == self._size:
            return
        self._size = (width, height)
        scale = self._get_widget_scaling()
        pil = _make_shadow_image(
            width, height, margin=SHADOW_MARGIN * scale, radius=self._radius * scale,
            blur=5.5 * scale, dy=7 * scale, opacity=0.6, bg=COLOR_BG,
        )
        self._image = ctk.CTkImage(pil, pil, size=(width / scale, height / scale))
        self._shadow.configure(image=self._image)


class ScrollableDropdown(ctk.CTkFrame):
    """A combo-box-style picker with a real, visible scrollbar in its popup.

    CTkComboBox's built-in dropdown is a native OS menu (tkinter.Menu) —
    with a long process list it *does* overflow-scroll on Windows, but only
    via tiny, easy-to-miss arrow hotspots at the very top/bottom, no visible
    scrollbar. This swaps that popup for a CTkToplevel containing a
    CTkScrollableFrame, which has an actual scrollbar track plus mouse-wheel
    support, and reads/writes the same way a combo box would (get/set/
    configure(values=...)) so call sites don't need to change.
    """

    def __init__(
        self,
        master,
        values: list[str] | None = None,
        height: int = 36,
        font: ctk.CTkFont | None = None,
        fg_color: str | None = None,
        border_color: str | None = None,
        text_color: str | None = None,
        button_color: str | None = None,
        button_hover_color: str | None = None,
        dropdown_fg_color: str | None = None,
        dropdown_hover_color: str | None = None,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)

        self._values: list[str] = list(values or [])
        self._selected = self._values[0] if self._values else ""
        self._popup: ctk.CTkToplevel | None = None
        self._font = font
        self._text_color = text_color
        self._dropdown_fg_color = dropdown_fg_color or fg_color
        self._dropdown_hover_color = dropdown_hover_color or button_color

        self._display = FadeButton(
            self, text=self._selected or "—", height=height, corner_radius=8,
            font=font, fg_color=fg_color, hover_color=fg_color, text_color=text_color,
            border_width=1, border_color=border_color, anchor="w",
            command=self._toggle_popup,
        )
        self._display.grid(row=0, column=0, sticky="ew")

        self._arrow = FadeButton(
            self, text="▾", width=28, height=height, corner_radius=8, font=font,
            fg_color=button_color, hover_color=button_hover_color, text_color=text_color,
            command=self._toggle_popup,
        )
        self._arrow.grid(row=0, column=1, padx=(4, 0))

    def get(self) -> str:
        return self._selected

    def set(self, value: str) -> None:
        self._selected = value
        self._display.configure(text=value or "—")

    def configure(self, **kwargs) -> None:
        if "values" in kwargs:
            self._values = list(kwargs.pop("values") or [])
        if kwargs:
            super().configure(**kwargs)

    def cget(self, attribute_name: str):
        if attribute_name == "values":
            return list(self._values)
        return super().cget(attribute_name)

    def _toggle_popup(self) -> None:
        if self._popup is not None:
            self._close_popup()
        else:
            self._open_popup()

    def _open_popup(self) -> None:
        if not self._values:
            return

        self.update_idletasks()
        x = self._display.winfo_rootx()
        y = self._display.winfo_rooty() + self._display.winfo_height() + 2
        width = self._display.winfo_width() + self._arrow.winfo_width() + 4
        row_height = 32
        popup_height = min(len(self._values) * row_height + 16, 320)

        popup = ctk.CTkToplevel(self)
        popup.overrideredirect(True)
        popup.geometry(f"{width}x{popup_height}+{x}+{y}")
        popup.attributes("-topmost", True)
        self._popup = popup

        scroll_frame = ctk.CTkScrollableFrame(
            popup, fg_color=self._dropdown_fg_color, corner_radius=6,
        )
        scroll_frame.pack(fill="both", expand=True, padx=1, pady=1)
        scroll_frame.grid_columnconfigure(0, weight=1)

        for i, value in enumerate(self._values):
            row = FadeButton(
                scroll_frame, parent_bg=self._dropdown_fg_color or COLOR_SURFACE, text=value, height=row_height - 4, corner_radius=4,
                font=self._font, fg_color="transparent", hover_color=self._dropdown_hover_color,
                text_color=self._text_color, anchor="w",
                command=lambda v=value: self._pick(v),
            )
            row.grid(row=i, column=0, sticky="ew", pady=1)

        popup.bind("<FocusOut>", lambda _e: self._close_popup())
        popup.bind("<Escape>", lambda _e: self._close_popup())
        popup.after(50, lambda: popup.focus_force())

    def _close_popup(self) -> None:
        if self._popup is not None:
            self._popup.destroy()
            self._popup = None

    def _pick(self, value: str) -> None:
        self.set(value)
        self._close_popup()


class App(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.attributes("-alpha", 0.0)  # faded in at the end of __init__
        self._tweens: dict = {}
        self._bar_values: dict = {}
        self._ambient_job = None
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
        self._boost_queue: queue.Queue = queue.Queue()
        self._process_name_by_display: dict[str, str] = {}

        self.sync_client: sync_client.SyncClient | None = None
        self.radio_player = radio_player.RadioPlayer(volume=70)
        self.radio_on = True
        self._loaded_track_url: str | None = None
        self._is_synced_playback = False
        self._track_load_queue: queue.Queue = queue.Queue()
        self._users_list_snapshot: tuple | None = None
        self._last_drift_correction = 0.0
        self._auto_skip_track_url: str | None = None
        self._local_queue: list[str] = []
        self._local_queue_index: int = 0
        self._synced_queue_position: tuple[int, int] | None = None
        self._loading_track = False
        self.game_watcher: optimizer.GameExitWatcher | None = None
        self._join_align_pending = False
        self._unboost_in_progress = False
        self._unboost_note: str | None = None
        self._boost_base_text = ""

        self._build_left_pane()
        self._build_right_pane()
        self._recover_from_previous_crash()

        self.stats_monitor.start()
        self._fade_in()
        self.after(2500, lambda: self.attributes("-alpha", 1.0))  # never leave the window invisible
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

    # ------------------------------------------------------------ animation
    # Everything here is cosmetic and deliberately cheap: short tweens at ~60fps
    # that finish in a fraction of a second, plus two slow ambient effects (the
    # Boost glow and the equalizer) that stop entirely while the window is
    # minimized - which is where it lives during a game.

    def _visible(self) -> bool:
        try:
            return self.state() == "normal"
        except tk.TclError:
            return False

    def _tween(self, key: str, start: float, end: float, ms: int, apply) -> None:
        """Ease apply(value) from start to end. A new tween with the same key replaces the old one."""
        job = self._tweens.pop(key, None)
        if job is not None:
            self.after_cancel(job)
        if ms <= 0 or not self._visible():
            apply(end)
            return
        t0 = time.monotonic()

        def step() -> None:
            progress = min((time.monotonic() - t0) * 1000 / ms, 1.0)
            apply(start + (end - start) * _ease_out(progress))
            if progress < 1.0:
                self._tweens[key] = self.after(16, step)
            else:
                self._tweens.pop(key, None)

        step()

    def _fade_in(self, attempt: int = 0) -> None:
        """Fade the window in - once it is actually on screen.

        CustomTkinter keeps a new window hidden for its first moments, so
        starting the tween immediately would just snap to fully opaque.
        """
        if self._visible() and attempt > 0:
            self._tween("fade", 0.0, 1.0, 320, lambda v: self.attributes("-alpha", v))
        elif attempt < 30:
            self.after(50, lambda: self._fade_in(attempt + 1))
        else:
            self.attributes("-alpha", 1.0)

    def _glide_bar(self, bar: ctk.CTkProgressBar, key: str, target: float) -> None:
        def apply(value: float) -> None:
            self._bar_values[key] = value
            bar.set(value)

        self._tween(key, self._bar_values.get(key, 0.0), target, 700, apply)

    def _title_underline(self, parent: ctk.CTkFrame, columnspan: int) -> None:
        """A short accent gradient beneath a section title."""
        width, height = 96, 3
        bar = _make_gradient_bar(width * 4, height * 4, COLOR_ACCENT_LIGHT, COLOR_CARD)
        image = ctk.CTkImage(bar, bar, size=(width, height))
        self._underline_images = getattr(self, "_underline_images", []) + [image]
        ctk.CTkLabel(parent, text="", image=image, height=height, fg_color="transparent").grid(
            row=1, column=0, columnspan=columnspan, sticky="w", pady=(4, 0)
        )

    def _draw_equalizer(self, playing: bool, now: float) -> None:
        canvas, s = self.eq_canvas, self._eq_scale
        height = int(canvas.cget("height"))
        bar_w, gap = int(3 * s), int(2 * s)
        canvas.delete("all")
        for i in range(4):
            if playing:
                level = 0.5 + 0.5 * math.sin(now * (4.0 + i * 1.3) + i * 1.9)
            else:
                level = self._eq_levels[i] * 0.55  # ease down to flat when audio stops
            self._eq_levels[i] = level
            bar_h = max(int(2 * s), int(level * height))
            x0 = i * (bar_w + gap)
            color = _lerp_color(COLOR_ACCENT, COLOR_ACCENT_LIGHT, i / 3) if playing or level > 0.1 else COLOR_BUSY
            canvas.create_rectangle(x0, height - bar_h, x0 + bar_w, height, fill=color, width=0)
        self._eq_moving = playing or max(self._eq_levels) > 0.08

    def _ensure_ambient(self) -> None:
        if self._ambient_job is None and self._visible():
            self._ambient_job = self.after(60, self._ambient_tick)

    def _ambient_tick(self) -> None:
        self._ambient_job = None
        if not self._visible():
            return  # minimized: stop; _poll_stats revives this when the window is back
        now = time.monotonic()
        boosted = self.boost_state is not None and not self._unboost_in_progress
        playing = self.radio_on and self.radio_player.is_playing()
        if boosted:
            glow = 0.5 + 0.5 * math.sin(now * 2.4)
            self.boost_button.configure(border_color=_lerp_color(COLOR_DANGER, "#F07C7E", glow))
        if playing or self._eq_moving:
            self._draw_equalizer(playing, now)
        if boosted or playing or self._eq_moving:
            self._ambient_job = self.after(70, self._ambient_tick)

    # ---------------------------------------------------------------- layout

    def _build_left_pane(self) -> None:
        left_shadow = ShadowFrame(self)
        left_shadow.grid(row=0, column=0, sticky="nsew", padx=(8, 0), pady=8)
        left = ctk.CTkFrame(
            left_shadow, corner_radius=16, fg_color=COLOR_CARD,
            border_width=1, border_color=COLOR_CARD_BORDER,
        )
        left.grid(row=0, column=0, sticky="nsew", padx=SHADOW_MARGIN, pady=SHADOW_MARGIN)
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
        self._title_underline(title_row, columnspan=3)

        ctk.CTkLabel(
            left,
            text="Pick a running game, then boost it.",
            font=ctk.CTkFont(family=FONT, size=12),
            text_color=COLOR_TEXT_MUTED,
        ).grid(row=1, column=0, sticky="w", padx=20, pady=(0, 12))

        selector_row = ctk.CTkFrame(left, fg_color="transparent")
        selector_row.grid(row=2, column=0, sticky="ew", padx=20, pady=4)
        selector_row.grid_columnconfigure(0, weight=1)

        self.game_dropdown = ScrollableDropdown(
            selector_row,
            values=["(click Refresh)"],
            height=36,
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
        FadeButton(
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

        self.boost_button = FadeButton(
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
            wraplength=400,
            justify="left",
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
        if self.sync_badge_label.cget("text") != text:
            self.sync_badge_label.configure(text=text, text_color=text_color)
        if self.sync_badge.cget("fg_color") != bg_color:
            self.sync_badge.configure(fg_color=bg_color)

    def _set_track_entry_mode(self, editable: bool, placeholder: str) -> None:
        """Only reconfigure the entry when its target state actually changes.

        _apply_sync_state() runs every 500ms via the sync poll loop.
        Unconditionally calling .configure() on a live text entry that often
        visually disrupts whatever the user is mid-typing or mid-pasting into
        it — even though the value being set is usually identical to what's
        already there. Querying the widget's actual current state first
        avoids ever touching it when nothing really changed.
        """
        target_state = "normal" if editable else "disabled"
        updates = {}
        if self.track_entry.cget("state") != target_state:
            updates["state"] = target_state
        if self.track_entry.cget("placeholder_text") != placeholder:
            updates["placeholder_text"] = placeholder
        if updates:
            self.track_entry.configure(**updates)

    @staticmethod
    def _set_button_state(button: ctk.CTkButton, state: str, text: str | None = None) -> None:
        """Same reasoning as _set_track_entry_mode, applied to buttons."""
        updates = {}
        if button.cget("state") != state:
            updates["state"] = state
        if text is not None and button.cget("text") != text:
            updates["text"] = text
        if updates:
            button.configure(**updates)

    def _queue_position_text(self) -> str:
        """' (2/23)' when playing from a multi-track queue, else empty."""
        if self._is_synced_playback:
            pos = self._synced_queue_position
            if pos and pos[1] > 1:
                return f" ({pos[0] + 1}/{pos[1]})"
        elif len(self._local_queue) > 1:
            return f" ({self._local_queue_index + 1}/{len(self._local_queue)})"
        return ""

    def _set_local_skip_button_state(self) -> None:
        """Skip only makes sense locally once there's more than one track to
        move to — a lone personal video has nowhere to skip forward to."""
        self._set_button_state(self.skip_button, "normal" if len(self._local_queue) > 1 else "disabled")

    def _build_right_pane(self) -> None:
        # Scrollable: a long track title or a busy listeners list pushes the
        # volume controls off the bottom, and a small window shouldn't force
        # anyone to resize it just to reach them.
        right_shadow = ShadowFrame(self)
        right_shadow.grid(row=0, column=1, sticky="nsew", padx=(0, 8), pady=8)
        content = ctk.CTkScrollableFrame(
            right_shadow, corner_radius=16, fg_color=COLOR_CARD,
            border_width=1, border_color=COLOR_CARD_BORDER,
            scrollbar_fg_color=COLOR_CARD,
            scrollbar_button_color=COLOR_BUSY,
            scrollbar_button_hover_color=COLOR_ACCENT,
        )
        self.right_pane = content
        content.grid(row=0, column=0, sticky="nsew", padx=SHADOW_MARGIN, pady=SHADOW_MARGIN)
        content.grid_columnconfigure(0, weight=1)

        title_row = ctk.CTkFrame(content, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="w", padx=20, pady=(4, 4))
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

        # Live equalizer: bars move while audio is actually playing.
        self._eq_scale = self._get_window_scaling()
        self._eq_levels = [0.0] * 4
        self._eq_moving = False
        self.eq_canvas = tk.Canvas(
            title_row, width=int(22 * self._eq_scale), height=int(16 * self._eq_scale),
            bg=COLOR_CARD, highlightthickness=0, bd=0,
        )
        self.eq_canvas.grid(row=0, column=3, padx=(14, 0), pady=(6, 0))
        self._draw_equalizer(False, 0.0)
        self._title_underline(title_row, columnspan=4)

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
        self.connect_button = FadeButton(
            connect_row, text="Connect", width=100, height=36, corner_radius=8,
            font=ctk.CTkFont(family=FONT, size=13),
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER, text_color=COLOR_BUTTON_TEXT,
            command=self._on_connect_clicked,
        )
        self.connect_button.grid(row=0, column=1, padx=(8, 0))

        self.admin_password_entry = ctk.CTkEntry(
            connect_row, placeholder_text="Admin password (optional)", show="*",
            height=32, corner_radius=8, font=ctk.CTkFont(family=FONT, size=12),
            fg_color=COLOR_SURFACE, border_color=COLOR_BORDER, text_color=COLOR_TEXT,
        )
        self.admin_password_entry.grid(row=1, column=0, sticky="ew", pady=(6, 0))

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
        self.track_action_button = FadeButton(
            track_row, text="Play Locally", width=110, height=36, corner_radius=8,
            font=ctk.CTkFont(family=FONT, size=13),
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER, text_color=COLOR_BUTTON_TEXT,
            command=self._on_track_action_clicked,
        )
        self.track_action_button.grid(row=0, column=1, padx=(8, 0))

        playback_row = ctk.CTkFrame(content, fg_color="transparent")
        playback_row.grid(row=10, column=0, sticky="ew", padx=20, pady=(8, 4))
        playback_row.grid_columnconfigure((0, 1), weight=1)
        self.play_pause_button = FadeButton(
            playback_row, text="▶ Play", height=36, corner_radius=8,
            font=ctk.CTkFont(family=FONT, size=13),
            fg_color="transparent", hover_color=COLOR_SURFACE,
            border_width=1, border_color=COLOR_BORDER, text_color=COLOR_TEXT,
            state="disabled", command=self._on_play_pause_clicked,
        )
        self.play_pause_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.skip_button = FadeButton(
            playback_row, text="⏭ Next", height=36, corner_radius=8,
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
        # Sorted heaviest-memory-first: a game is almost always the single
        # biggest RAM consumer running, so this puts it right at the top
        # instead of making the user hunt for it alphabetically. Processes
        # sharing a name (e.g. several chrome.exe helper processes) have
        # their memory summed, so multi-process apps rank by their true
        # total footprint rather than one arbitrary instance's share of it.
        memory_by_name: dict[str, int] = {}
        for p in psutil.process_iter(["name", "memory_info"]):
            name = p.info["name"]
            if not name or not name.lower().endswith(".exe"):
                continue
            mem = p.info["memory_info"]
            memory_by_name[name] = memory_by_name.get(name, 0) + (mem.rss if mem else 0)

        ranked = sorted(memory_by_name.items(), key=lambda pair: pair[1], reverse=True)
        self._process_name_by_display = {
            f"{name}   ({_format_bytes(mem)})": name for name, mem in ranked
        }
        display_values = list(self._process_name_by_display)
        self.game_dropdown.configure(values=display_values or ["(no processes found)"])
        if display_values:
            self.game_dropdown.set(display_values[0])

    # ------------------------------------------------------------ boost flow

    def _on_boost_clicked(self) -> None:
        if self.boost_state is not None:
            self._start_unboost()
        else:
            self._start_boost()

    def _start_boost(self) -> None:
        selection = self.game_dropdown.get()
        if not selection or selection.startswith("("):
            self.status_label.configure(text="Pick a running .exe first.", text_color=COLOR_WARNING)
            return
        target = self._process_name_by_display.get(selection, selection)

        self.boost_button.configure(
            state="disabled", text="Boosting...", fg_color=COLOR_BUSY, hover_color=COLOR_BUSY
        )
        self.status_label.configure(text=f"Boosting '{target}'...", text_color=COLOR_TEXT_MUTED)

        def worker() -> None:
            try:
                state = optimizer.optimize_for_game(target)
                reason = None if state is not None else optimizer.explain_boost_failure(target)
            except Exception:
                # Never leave the button stuck on "Boosting..." if something
                # unexpected goes wrong - report it as a plain failure.
                logger.exception("Boost failed unexpectedly")
                state, reason = None, "unknown"
            self._boost_queue.put(("boost_done", target, state, reason))
            if state is not None:
                # Reported separately, after the boost is already in effect:
                # the cleanup runs in Windows' low-priority background mode and
                # may take a while - the boost itself never waits on the disk.
                try:
                    stats = optimizer.clear_temp_folders()
                except Exception:
                    logger.exception("Temp cleanup failed")
                    stats = {"files_removed": 0, "bytes_freed": 0}
                self._boost_queue.put(("temp_done", target, state, stats))

        threading.Thread(target=worker, daemon=True).start()
        self.after(QUEUE_POLL_MS, self._poll_boost_queue)

    def _start_unboost(self) -> None:
        self._unboost_in_progress = True
        self.boost_button.configure(
            state="disabled", text="Restoring...", fg_color=COLOR_BUSY, hover_color=COLOR_BUSY
        )
        if self.game_watcher is not None:
            self.game_watcher.stop()
            self.game_watcher = None

        state = self.boost_state

        def worker() -> None:
            try:
                optimizer.restore_defaults(state)
            except Exception:
                logger.exception("Restore failed unexpectedly")
            finally:
                self._boost_queue.put(("unboost_done", None, None, None))

        threading.Thread(target=worker, daemon=True).start()
        self.after(QUEUE_POLL_MS, self._poll_boost_queue)

    def _poll_boost_queue(self) -> None:
        try:
            kind, target, state, extra = self._boost_queue.get_nowait()
        except queue.Empty:
            self.after(QUEUE_POLL_MS, self._poll_boost_queue)
            return

        if kind == "boost_done":
            if state is None:
                if extra == "needs_admin":
                    text = (
                        f"'{target}' runs as administrator, so Windows won't let Game Booster "
                        "change it. Restart Game Booster as administrator to boost it."
                    )
                else:
                    text = f"Could not boost '{target}'. Is it still running?"
                self.status_label.configure(text=text, text_color=COLOR_WARNING)
                self.boost_button.configure(
                    state="normal", text="⚡ BOOST GAME",
                    fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
                )
            else:
                self.boost_state = state
                self.game_watcher = optimizer.GameExitWatcher(state.game_pid)
                self.game_watcher.start()
                power_note = " High Performance power plan on." if state.original_power_scheme else ""
                self._boost_base_text = (
                    f"Boosted '{target}' — {len(state.original_priorities)} "
                    f"background processes lowered.{power_note}"
                )
                self.status_label.configure(text=self._boost_base_text, text_color=COLOR_SUCCESS)
                self.boost_button.configure(
                    state="normal", text="⏹ UN-BOOST",
                    fg_color=COLOR_DANGER, hover_color=COLOR_DANGER_HOVER,
                    border_width=2, border_color=COLOR_DANGER,
                )
                self._ensure_ambient()
                # Keep listening: the temp-cleanup result follows shortly.
                self.after(QUEUE_POLL_MS, self._poll_boost_queue)
        elif kind == "temp_done":
            if self.boost_state is not None and not self._unboost_in_progress:
                count = extra["files_removed"]
                if count > 0:
                    noun = "temp file" if count == 1 else "temp files"
                    self.status_label.configure(
                        text=f"{self._boost_base_text} Cleared {count} {noun} "
                             f"({_format_bytes(extra['bytes_freed'])}).",
                        text_color=COLOR_SUCCESS,
                    )
        elif kind == "unboost_done":
            self.boost_state = None
            self._unboost_in_progress = False
            note, self._unboost_note = self._unboost_note, None
            self.status_label.configure(
                text=note or "Idle — no game boosted.", text_color=COLOR_TEXT_MUTED
            )
            self.boost_button.configure(
                state="normal", text="⚡ BOOST GAME",
                fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
                border_width=0,
            )

    def _auto_unboost(self) -> None:
        """The boosted game exited on its own: put everything back right away."""
        self._unboost_note = "Game closed — everything restored automatically."
        self._start_unboost()

    # ------------------------------------------------------------- polling

    def _poll_stats(self) -> None:
        watcher = self.game_watcher
        if (
            watcher is not None and watcher.exited.is_set()
            and self.boost_state is not None and not self._unboost_in_progress
        ):
            self._auto_unboost()

        # Nobody is looking at these numbers while the window is minimized
        # (which is where it lives during a game), so skip redrawing them and
        # slow the sampler down too - fewer wakeups on the game's cores.
        hidden = self.state() in ("iconic", "withdrawn")
        if not hidden:
            self._ensure_ambient()  # revives glow/equalizer after a minimize
        self.stats_monitor.interval_seconds = 5.0 if hidden else 1.0
        stats = None if hidden else self.stats_monitor.get_latest()
        if stats is not None:
            self.cpu_label.configure(text=f"CPU: {stats.cpu_percent:.0f}%")
            self._glide_bar(self.cpu_bar, "cpu", stats.cpu_percent / 100)
            self.cpu_bar.configure(progress_color=_load_bar_color(stats.cpu_percent))

            self.ram_label.configure(
                text=f"RAM: {stats.ram_percent:.0f}% ({stats.ram_used_gb:.1f} / {stats.ram_total_gb:.1f} GB)"
            )
            self._glide_bar(self.ram_bar, "ram", stats.ram_percent / 100)
            self.ram_bar.configure(progress_color=_load_bar_color(stats.ram_percent))
        self.after(STATS_HIDDEN_REFRESH_MS if hidden else STATS_REFRESH_MS, self._poll_stats)

    # --------------------------------------------------------------- sync UI

    def _server_uri(self) -> str:
        return self.server_entry.get().strip() or DEFAULT_SERVER_URI

    def _on_connect_clicked(self) -> None:
        if self.sync_client is not None:
            self._disconnect()
            return
        password = self.admin_password_entry.get().strip()
        hello = {"name": "Guest", "password": password} if password else {"name": "Guest"}
        self.sync_client = sync_client.SyncClient(self._server_uri(), hello=hello)
        self.sync_client.start()
        self.connect_button.configure(text="Disconnect")
        self.server_entry.configure(state="disabled")
        self.admin_password_entry.configure(state="disabled")

    def _disconnect(self) -> None:
        if self.sync_client is not None:
            self.sync_client.stop()
            self.sync_client = None
        self.connect_button.configure(text="Connect")
        self.server_entry.configure(state="normal")
        self.admin_password_entry.configure(state="normal")
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
        server_position = _server_position_now(state)

        self._update_users_list(state.get("users", []))
        self._synced_queue_position = (
            (state.get("queue_index", 0), state.get("queue_length", 0)) if sync_active else None
        )

        if sync_active:
            self._set_sync_badge("🔒 SYNCED TO ADMIN STREAM", COLOR_SUCCESS, COLOR_BADGE_BG_SUCCESS)

            if track_url and (not self._is_synced_playback or track_url != self._loaded_track_url):
                self._is_synced_playback = True
                self._loaded_track_url = track_url
                self._auto_skip_track_url = None
                self._start_load_track(
                    track_url, autoplay=server_is_playing and self.radio_on
                )
            elif self._is_synced_playback and self.radio_on and not self._loading_track:
                # (Nothing to do while a track is still loading: the player is
                # empty, so there is no position to correct and any seek or
                # play() would just be thrown away.)
                # A playlist track that's played through to the end looks the
                # same to VLC as one that's simply paused (is_playing() is
                # False either way) — checked first so the block below
                # doesn't try to restart the just-ended track from the top
                # instead of letting it advance to the next one.
                ended = self.radio_player.is_ended()
                if is_admin and ended and state.get("queue_length", 0) > 1:
                    if self._auto_skip_track_url != self._loaded_track_url:
                        self._auto_skip_track_url = self._loaded_track_url
                        self.sync_client.send({"type": "skip"})
                else:
                    if server_is_playing and not self.radio_player.is_playing() and not ended:
                        self.radio_player.play()
                    elif not server_is_playing and self.radio_player.is_playing():
                        self.radio_player.pause()
                    self._correct_drift(server_position, server_is_playing)

            self._set_track_entry_mode(
                is_admin, "Paste a YouTube link..." if is_admin else "An admin stream is live"
            )
            self._set_button_state(self.track_action_button, "normal" if is_admin else "disabled", "Sync for Everyone")
            self._set_button_state(self.play_pause_button, "normal" if is_admin else "disabled")
            self._set_button_state(
                self.skip_button,
                "normal" if is_admin and state.get("queue_length", 0) > 1 else "disabled",
            )
        else:
            if self._is_synced_playback:
                self.radio_player.stop()
                self._is_synced_playback = False
                self._loaded_track_url = None
                self._local_queue = []
                self._local_queue_index = 0
                self.track_label.configure(text="No track loaded.")

            self._set_sync_badge("🎧 LOCAL PLAYER MODE", COLOR_ACCENT_LIGHT, COLOR_BADGE_BG_ACCENT)
            self._set_track_entry_mode(True, "Paste a YouTube link...")
            self._set_button_state(
                self.track_action_button, "normal", "Sync for Everyone" if is_admin else "Play Locally"
            )
            self._set_button_state(self.play_pause_button, "normal" if self._loaded_track_url else "disabled")
            self._set_local_skip_button_state()

    def _correct_drift(self, server_position: float, playing: bool = True) -> None:
        if self.radio_player.is_buffering():
            return  # position reading is unreliable mid-buffer; don't chase it

        if self._join_align_pending and not self.radio_player.is_playing():
            return  # still opening/starting - a seek now would be ignored

        drift = server_position - self.radio_player.get_position_seconds()  # + = we're behind

        if self._join_align_pending:
            self._join_align_pending = False
            if abs(drift) > DRIFT_JOIN_SEEK_THRESHOLD_SECONDS:
                self.radio_player.seek(server_position)
                self.radio_player.set_rate(1.0)
                self._last_drift_correction = time.monotonic()
                return

        if abs(drift) > DRIFT_SEEK_THRESHOLD_SECONDS:
            now = time.monotonic()
            if now - self._last_drift_correction < DRIFT_SEEK_COOLDOWN_SECONDS:
                return  # give the last seek time to actually settle first
            self.radio_player.seek(server_position)
            self.radio_player.set_rate(1.0)
            self._last_drift_correction = now
            return

        if not playing:
            self.radio_player.set_rate(1.0)  # nothing to catch up while paused
        elif abs(drift) >= DRIFT_NUDGE_START_SECONDS:
            self.radio_player.set_rate(1.0 + DRIFT_NUDGE_RATE if drift > 0 else 1.0 - DRIFT_NUDGE_RATE)
        elif abs(drift) <= DRIFT_NUDGE_STOP_SECONDS:
            self.radio_player.set_rate(1.0)
        # in between: keep doing whatever we're already doing (hysteresis)

    def _start_load_track(self, url: str, autoplay: bool = True) -> None:
        self.track_label.configure(text=f"Loading '{url}'...")
        # The player still reports the PREVIOUS track as Ended until the new
        # one finishes loading; auto-advance must not read that as another end.
        self._loading_track = True

        def worker() -> None:
            try:
                info = self.radio_player.load(url)
                self._track_load_queue.put(("load_done", info, autoplay, None))
            except Exception as exc:  # ExtractionError, or anything VLC/yt-dlp throws
                self._track_load_queue.put(("load_failed", None, None, str(exc)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(QUEUE_POLL_MS, self._poll_track_load_queue)

    def _poll_track_load_queue(self) -> None:
        try:
            kind, info, autoplay, error = self._track_load_queue.get_nowait()
        except queue.Empty:
            self.after(QUEUE_POLL_MS, self._poll_track_load_queue)
            return

        if kind == "playlist_resolved":
            # The "info" slot carries the resolved list[str] of URLs here
            # instead of the usual extract_audio_url() info dict — this
            # message only ever comes from _resolve_and_sync_playlist(),
            # never from _start_load_track(), so there's no ambiguity.
            urls = info
            if self.sync_client is not None:
                self.sync_client.send({"type": "set_playlist", "urls": urls})
            return

        if kind == "local_playlist_resolved":
            # Same shape as "playlist_resolved" above, but for personal
            # playback: no server involved, so the resolved URLs become a
            # purely client-side queue that Skip/auto-play walk through
            # locally, independent of anyone else's admin stream.
            urls = info
            self._is_synced_playback = False
            self._local_queue = urls
            self._local_queue_index = 0
            self._loaded_track_url = urls[0]
            self._start_load_track(urls[0], autoplay=True)
            return

        if kind in ("load_done", "load_failed"):
            self._loading_track = False

        if kind == "load_done":
            self.track_label.configure(text=f"Now playing{self._queue_position_text()}: {info['title']}")
            if autoplay:
                self.radio_player.play()
                # VLC silently ignores a seek made before a network stream has
                # started playing (measured: a listener joining mid-track just
                # started from 0:00), so the join alignment is deferred until
                # playback is actually running - see _correct_drift().
                self._join_align_pending = self._is_synced_playback
            self.play_pause_button.configure(text="⏸ Pause" if autoplay else "▶ Play")
            if not self._is_synced_playback:
                # A locally-driven load (no admin stream involved) — this is
                # the only client controlling it, so it's always fine to
                # enable these here. A synced load's button state is instead
                # decided solely by _apply_sync_state's role check on its own
                # poll tick, so this stays out of the way of that gating.
                self._set_button_state(self.play_pause_button, "normal")
                self._set_local_skip_button_state()
        elif kind == "load_failed":
            logger.warning("Track load failed: %s", error)
            self.track_label.configure(
                text="Could not load that link — it may be unavailable, private, or region-locked."
            )
            self._is_synced_playback = False
            self._loaded_track_url = None
            self._local_queue = []
            self._local_queue_index = 0
            self._set_button_state(self.play_pause_button, "disabled")
            self._set_local_skip_button_state()

    def _on_track_action_clicked(self) -> None:
        url = self.track_entry.get().strip()
        if not url:
            self.track_label.configure(text="Paste a YouTube link first.")
            return

        state = self.sync_client.get_latest_state() if self.sync_client else None
        is_admin = bool(state and state.get("your_role") == "admin")
        sync_active = bool(state and state.get("sync_active"))

        if is_admin:
            self.track_entry.delete(0, "end")
            self._resolve_and_sync_playlist(url)
        elif not sync_active:
            self.track_entry.delete(0, "end")
            self._resolve_and_play_locally(url)

    def _resolve_and_play_locally(self, url: str) -> None:
        """Expand a playlist link (if it is one) into a personal queue.

        Purely client-side — nothing here touches the server, so this is
        available with or without a connection at all. A plain single-video
        link resolves to a one-track "queue" with no network call (see
        resolve_track_urls), same as before this queue concept existed.
        """
        self.track_label.configure(text=f"Loading '{url}'...")

        def worker() -> None:
            try:
                urls = radio_player.resolve_track_urls(url)
                self._track_load_queue.put(("local_playlist_resolved", urls, None, None))
            except Exception as exc:  # ExtractionError, or anything VLC/yt-dlp throws
                self._track_load_queue.put(("load_failed", None, None, str(exc)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(QUEUE_POLL_MS, self._poll_track_load_queue)

    def _advance_local_queue(self) -> None:
        if len(self._local_queue) < 2:
            return
        self._local_queue_index = (self._local_queue_index + 1) % len(self._local_queue)
        next_url = self._local_queue[self._local_queue_index]
        self._loaded_track_url = next_url
        self._start_load_track(next_url, autoplay=True)

    def _resolve_and_sync_playlist(self, url: str) -> None:
        """Expand a playlist link to its videos, then sync the whole thing.

        A plain single-video link resolves to itself with no network call
        (see resolve_track_urls), so this adds no delay for the common
        case — only an actual playlist link takes the extra round trip.
        Runs on a background thread for the same reason _start_load_track
        does: it may be a real network request, and blocking the GUI
        thread on one would freeze the whole window.
        """
        self.track_label.configure(text=f"Loading '{url}'...")

        def worker() -> None:
            try:
                urls = radio_player.resolve_track_urls(url)
                self._track_load_queue.put(("playlist_resolved", urls, None, None))
            except Exception as exc:  # ExtractionError, or anything VLC/yt-dlp throws
                self._track_load_queue.put(("load_failed", None, None, str(exc)))

        threading.Thread(target=worker, daemon=True).start()
        self.after(QUEUE_POLL_MS, self._poll_track_load_queue)

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
        state = self.sync_client.get_latest_state() if self.sync_client else None
        if state and state.get("your_role") == "admin" and state.get("sync_active"):
            self.sync_client.send({"type": "skip"})
        else:
            self._advance_local_queue()

    def _on_promote_clicked(self, user_id: int) -> None:
        if self.sync_client is not None:
            self.sync_client.send({"type": "promote", "target_id": user_id})

    def _update_users_list(self, users: list[dict]) -> None:
        """Rebuild the listener list — but only when it actually changed.

        This is called from _apply_sync_state() on every 500ms poll tick.
        Unconditionally destroying and recreating every widget here that
        often caused a constant visible flicker (and, worse, a layout
        reflow of everything below it in the same column — the track entry
        and buttons — that could interrupt an in-progress paste). Skipping
        the rebuild entirely when nothing changed fixes both.
        """
        my_state = self.sync_client.get_latest_state() if self.sync_client else None
        my_id = my_state.get("your_id") if my_state else None
        i_am_admin = bool(my_state and my_state.get("your_role") == "admin")

        snapshot = (i_am_admin, my_id, tuple((u["id"], u["name"], u["role"]) for u in users))
        if snapshot == self._users_list_snapshot:
            return
        self._users_list_snapshot = snapshot

        for widget in self.users_frame.winfo_children():
            widget.destroy()

        if not users:
            ctk.CTkLabel(
                self.users_frame, text="Not connected.",
                font=ctk.CTkFont(family=FONT, size=12), text_color=COLOR_TEXT_MUTED,
            ).grid(row=0, column=0, sticky="w")
            return

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
                FadeButton(
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
                        self._correct_drift(_server_position_now(state), state.get("is_playing", False))
        else:
            self.radio_player.pause()

    def _on_volume_change(self, value: float) -> None:
        self.radio_player.set_volume(int(value))

    # ---------------------------------------------------------------- close

    def _on_close(self) -> None:
        self.stats_monitor.stop()
        if self.game_watcher is not None:
            self.game_watcher.stop()
        if self.boost_state is not None:
            optimizer.restore_defaults(self.boost_state)
        if self.sync_client is not None:
            self.sync_client.stop()
        self.radio_player.release()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
