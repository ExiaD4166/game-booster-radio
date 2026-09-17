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
