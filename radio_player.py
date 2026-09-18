"""Audio-only YouTube radio player.

Resolves a YouTube link to a direct audio stream with yt-dlp (no download, no
video) and plays it with VLC. Every method here does its real work directly
and blocks until done — it does not manage its own thread. Running the slow
parts (extraction is a real network request) on a background thread is the
caller's job, the same way optimizer.py's optimize_for_game() is a blocking
call its caller is responsible for threading.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

if getattr(sys, "frozen", False):
    # Running from a PyInstaller-built .exe: point python-vlc at the bundled
    # copy of libvlc instead of letting it search the system (a friend
    # running the packaged app won't have VLC installed at all). These two
    # environment variables are exactly what vlc.py's find_lib() checks
    # first, before falling back to the registry/PATH search — and it does
    # that check at import time, so this must run before `import vlc` below.
    # sys._MEIPASS is where PyInstaller actually places bundled data/binaries
    # in this version's onedir layout (an _internal/ folder next to the exe,
    # not the exe's own directory — confirmed by inspecting the built output).
    _vlc_runtime = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "vlc_runtime"
    os.environ.setdefault("PYTHON_VLC_LIB_PATH", str(_vlc_runtime / "libvlc.dll"))
    os.environ.setdefault("PYTHON_VLC_MODULE_PATH", str(_vlc_runtime / "plugins"))

import vlc
import yt_dlp

logger = logging.getLogger(__name__)


class ExtractionError(Exception):
    """Raised when yt-dlp can't resolve a playable audio URL for a link."""


class _SilentYtDlpLogger:
    """Swallows yt-dlp's own console output; failures surface via ExtractionError."""

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        pass

    def error(self, msg: str) -> None:
        logger.debug("yt-dlp error: %s", msg)


def extract_audio_url(youtube_url: str) -> dict:
    """Resolve a YouTube link to a direct, audio-only stream URL.

    Returns {"url", "title", "duration"}. Blocking — makes a real network
    request; call from a background thread if calling from a GUI.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "logger": _SilentYtDlpLogger(),
        "format": "bestaudio/best",
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
    except yt_dlp.utils.DownloadError as exc:
        raise ExtractionError(f"Could not extract audio from '{youtube_url}': {exc}") from exc

    stream_url = info.get("url")
    if not stream_url:
        raise ExtractionError(f"yt-dlp found no playable audio stream for '{youtube_url}'.")

    return {
        "url": stream_url,
        "title": info.get("title", "Unknown title"),
        "duration": info.get("duration"),
    }


def _playlist_id(url: str) -> str | None:
    """The playlist id in a link, or None if it shouldn't be expanded.

    Covers both a playlist page ('.../playlist?list=Y') and a video that sits
    inside one ('.../watch?v=X&list=Y' — what you get copying the address bar
    while a playlist is playing). YouTube "Mix" lists (ids starting 'RD') are
    auto-generated, personalized and endless, so those stay single-video.
    """
    ids = parse_qs(urlparse(url).query).get("list")
    if not ids or ids[0].startswith("RD"):
        return None
    return ids[0]


def resolve_track_urls(url: str) -> list[str]:
    """Resolve a pasted link to one or more individual, single-video URLs.

    A plain video link resolves to itself with no network call. A link that
    carries a playlist id is expanded to every video in that playlist via
    yt-dlp's flat extraction (it only reads the playlist page — ids and
    titles — not each video's audio stream, so it's fast even for long
    lists). If the link also names a specific video, the queue is rotated to
    START at that video (then continues through the rest and wraps around).
    Each returned URL is later resolved individually by extract_audio_url()
    only when it's actually that track's turn to play.
    """
    playlist_id = _playlist_id(url)
    if playlist_id is None:
        return [url]

    pasted_video = parse_qs(urlparse(url).query).get("v", [None])[0]
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "logger": _SilentYtDlpLogger(),
        "extract_flat": "in_playlist",
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/playlist?list={playlist_id}", download=False
            )
        entries = info.get("entries") if info else None
    except yt_dlp.utils.DownloadError as exc:
        if pasted_video:
            return [url]  # playlist unreadable (private?) — still play the video
        raise ExtractionError(f"Could not read playlist '{url}': {exc}") from exc

    ids: list[str] = []
    for entry in entries or []:
        if entry and entry.get("id"):
            ids.append(entry["id"])

    if not ids:
        if pasted_video:
            return [url]
        raise ExtractionError(f"'{url}' doesn't look like a playlist with any videos.")

    if pasted_video in ids:
        start = ids.index(pasted_video)
        ids = ids[start:] + ids[:start]
    return [f"https://www.youtube.com/watch?v={video_id}" for video_id in ids]


class RadioPlayer:
    """Controls one audio-only stream via VLC.

    Not thread-safe against concurrent calls from multiple threads — the
    caller is expected to serialize access (e.g. one dedicated player thread
    that owns this object).
    """

    def __init__(self, volume: int = 70) -> None:
        self._instance = vlc.Instance("--no-video", "--quiet")
        self._player = self._instance.media_player_new()
        self.title: str | None = None
        self.duration_seconds: float | None = None
        self.set_volume(volume)

    def load(self, youtube_url: str) -> dict:
        """Extract and load a YouTube link, ready to play(). Blocking."""
        info = extract_audio_url(youtube_url)
        media = self._instance.media_new(info["url"])
        self._player.set_media(media)
        self.title = info["title"]
        self.duration_seconds = info["duration"]
        return info

    def play(self) -> None:
        self._player.play()

    def pause(self) -> None:
        self._player.set_pause(1)

    def resume(self) -> None:
        self._player.set_pause(0)

    def stop(self) -> None:
        self._player.stop()

    def is_playing(self) -> bool:
        return bool(self._player.is_playing())

    def is_buffering(self) -> bool:
        """True while VLC is buffering or opening a stream.

        Position reporting during this window is unreliable (it can stall
        or jump once buffering finishes), so callers doing drift correction
        should skip measuring/correcting entirely while this is true rather
        than react to a stale or about-to-jump reading.
        """
        return self._player.get_state() in (vlc.State.Buffering, vlc.State.Opening)

    def is_ended(self) -> bool:
        """True once VLC has played a loaded track through to its end."""
        return self._player.get_state() == vlc.State.Ended

    def get_position_seconds(self) -> float:
        return max(self._player.get_time(), 0) / 1000.0

    def seek(self, position_seconds: float) -> None:
        self._player.set_time(int(position_seconds * 1000))

    def set_volume(self, percent: int) -> None:
        self._player.audio_set_volume(max(0, min(100, percent)))

    def get_volume(self) -> int:
        return self._player.audio_get_volume()

    def release(self) -> None:
        """Release VLC resources. Call once when this player is no longer needed."""
        self._player.release()
        self._instance.release()


if __name__ == "__main__":
    import sys
    import time

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if len(sys.argv) != 2:
        print("Usage: python radio_player.py <youtube_url>")
        raise SystemExit(1)

    player = RadioPlayer()
    try:
        print(f"Extracting audio from '{sys.argv[1]}'...")
        info = player.load(sys.argv[1])
        print(f"Loaded: {info['title']} ({info['duration']}s)")
    except ExtractionError as exc:
        print(f"Extraction failed: {exc}")
        raise SystemExit(1)

    player.play()
    print("Playing. Press Enter to stop.")
    time.sleep(1)  # give VLC a moment to start before reporting position
    print(f"Position: {player.get_position_seconds():.1f}s, volume: {player.get_volume()}%")
    input()
    player.stop()
    player.release()
