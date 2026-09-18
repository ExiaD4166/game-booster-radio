"""WebSocket client for the sync radio, driven from a GUI thread.

Runs its own asyncio event loop on a dedicated background thread — Tkinter
already owns the main thread's event loop, so the network connection needs
an independent one. Talking across that thread boundary uses the same shape
as StatsMonitor: the network thread writes the latest received state behind
a lock, and the GUI thread polls it via self.after(...). Sending a command
in the other direction goes through asyncio.run_coroutine_threadsafe().
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading

import websockets

logger = logging.getLogger(__name__)

RECONNECT_DELAY_SECONDS = 3.0


class SyncClient:
    """Connects to server.py, auto-reconnecting if the connection drops."""

    def __init__(self, uri: str) -> None:
        self.uri = uri
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ws: websockets.ClientConnection | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._latest_state: dict | None = None
        self._connected = False

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._loop is not None and self._ws is not None:
            asyncio.run_coroutine_threadsafe(self._close_ws(), self._loop)
        if self._thread is not None:
            self._thread.join(timeout=RECONNECT_DELAY_SECONDS + 2)
            self._thread = None

    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def get_latest_state(self) -> dict | None:
        with self._lock:
            return self._latest_state

    def send(self, message: dict) -> None:
        """Queue a command to be sent. Safe to call from the GUI thread."""
        if self._loop is None:
            logger.warning("Cannot send %s: client not started.", message.get("type"))
            return
        asyncio.run_coroutine_threadsafe(self._send_async(message), self._loop)

    async def _send_async(self, message: dict) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps(message))
        except websockets.ConnectionClosed:
            pass

    async def _close_ws(self) -> None:
        if self._ws is not None:
            await self._ws.close()

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_loop())
        finally:
            self._loop.close()
            self._loop = None

    async def _connect_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(self.uri, open_timeout=5) as ws:
                    self._ws = ws
                    with self._lock:
                        self._connected = True
                    logger.info("Connected to %s", self.uri)
                    async for raw in ws:
                        self._handle_message(raw)
            except Exception as exc:
                logger.warning("Sync connection error: %s", exc)
            finally:
                self._ws = None
                with self._lock:
                    self._connected = False

            if self._stop_event.is_set():
                break
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    def _handle_message(self, raw: str) -> None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        if data.get("type") == "state":
            with self._lock:
                self._latest_state = data


if __name__ == "__main__":
    import sys
    import time

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    uri = sys.argv[1] if len(sys.argv) > 1 else "ws://localhost:8765"
    client = SyncClient(uri)
    client.start()

    print(f"Connecting to {uri} ... (Ctrl+C to quit)")
    try:
        while True:
            time.sleep(1)
            state = client.get_latest_state()
            print(f"connected={client.is_connected()} state={state}")
    except KeyboardInterrupt:
        pass
    finally:
        client.stop()
