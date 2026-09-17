"""WebSocket sync server for the shared radio.

Tracks connected clients, their roles (admin vs. user), and one shared
"now playing" state. Broadcasts that state to everyone whenever it changes,
and again on a timer so clients can catch up if they missed an update.

This server only tracks and enforces WHO may change the shared state — it does
not decide what a normal user's local player does in response. That decision
("stop my own track, an admin stream is live") is client-side logic, built in
a later phase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from itertools import count

import websockets

logger = logging.getLogger(__name__)

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", 8765))
BROADCAST_INTERVAL_SECONDS = 3.0

ADMIN_ONLY_MESSAGE_TYPES = {"set_playlist", "play", "pause", "seek", "skip", "stop_sync", "promote"}

_next_client_id = count(1)


@dataclass
class Client:
    id: int
    connection: websockets.ServerConnection
    name: str = "Guest"
    role: str = "user"  # "admin" or "user"


@dataclass
class RadioState:
    """The one shared 'now playing' state every client sees the same version of."""

    queue: list[str] = field(default_factory=list)
    current_index: int = 0
    is_playing: bool = False
    position_seconds: float = 0.0
    last_changed: float = field(default_factory=time.monotonic)
    sync_active: bool = False

    def current_track(self) -> str | None:
        if not self.queue:
            return None
        return self.queue[self.current_index % len(self.queue)]

    def current_position(self) -> float:
        if not self.is_playing:
            return self.position_seconds
        return self.position_seconds + (time.monotonic() - self.last_changed)


clients: dict[int, Client] = {}
state = RadioState()


def _has_admin() -> bool:
    return any(client.role == "admin" for client in clients.values())


def _state_payload(recipient: Client) -> str:
    return json.dumps(
        {
            "type": "state",
            "sync_active": state.sync_active,
            "track_url": state.current_track(),
            "position": round(state.current_position(), 2),
            "is_playing": state.is_playing,
            "queue_length": len(state.queue),
            "your_id": recipient.id,
            "your_role": recipient.role,
            "users": [
                {"id": c.id, "name": c.name, "role": c.role} for c in clients.values()
            ],
        }
    )


async def _broadcast_state() -> None:
    if not clients:
        return
    await asyncio.gather(
        *(c.connection.send(_state_payload(c)) for c in clients.values()),
        return_exceptions=True,
    )


async def _heartbeat() -> None:
    while True:
        await asyncio.sleep(BROADCAST_INTERVAL_SECONDS)
        await _broadcast_state()


async def _send_error(client: Client, message: str) -> None:
    try:
        await client.connection.send(json.dumps({"type": "error", "message": message}))
    except websockets.ConnectionClosed:
        pass


async def _handle_message(client: Client, raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        await _send_error(client, "Malformed message; expected JSON.")
        return

    msg_type = msg.get("type")

    if msg_type == "hello":
        client.name = str(msg.get("name", "Guest"))[:40]
        await _broadcast_state()
        return

    if msg_type in ADMIN_ONLY_MESSAGE_TYPES and client.role != "admin":
        await _send_error(client, "Only an admin can do that.")
        return

    if msg_type == "set_playlist":
        urls = msg.get("urls")
        if not isinstance(urls, list) or not urls:
            await _send_error(client, "set_playlist needs a non-empty list of urls.")
            return
        state.queue = [str(url) for url in urls]
        state.current_index = 0
        state.position_seconds = 0.0
        state.last_changed = time.monotonic()
        state.is_playing = True
        state.sync_active = True

    elif msg_type == "play":
        if not state.is_playing:
            state.is_playing = True
            state.last_changed = time.monotonic()

    elif msg_type == "pause":
        if state.is_playing:
            state.position_seconds = state.current_position()
            state.is_playing = False
            state.last_changed = time.monotonic()

    elif msg_type == "seek":
        position = msg.get("position")
        if not isinstance(position, (int, float)) or position < 0:
            await _send_error(client, "seek needs a non-negative 'position'.")
            return
        state.position_seconds = float(position)
        state.last_changed = time.monotonic()

    elif msg_type == "skip":
        if not state.queue:
            await _send_error(client, "Queue is empty.")
            return
        state.current_index = (state.current_index + 1) % len(state.queue)
        state.position_seconds = 0.0
        state.last_changed = time.monotonic()

    elif msg_type == "stop_sync":
        state.sync_active = False
        state.is_playing = False

    elif msg_type == "promote":
        target_id = msg.get("target_id")
        target = clients.get(target_id)
        if target is None:
            await _send_error(client, f"No connected user with id {target_id}.")
            return
        target.role = "admin"

    else:
        await _send_error(client, f"Unknown message type '{msg_type}'.")
        return

    await _broadcast_state()


async def _handle_client(connection: websockets.ServerConnection) -> None:
    client = Client(id=next(_next_client_id), connection=connection)
    if not _has_admin():
        client.role = "admin"
    clients[client.id] = client
    logger.info("Client %s connected as %s (%d total).", client.id, client.role, len(clients))

    try:
        await connection.send(_state_payload(client))
        async for raw in connection:
            try:
                await _handle_message(client, raw)
            except Exception:
                logger.exception("Error handling message from client %s", client.id)
                await _send_error(client, "Internal server error handling your message.")
    except websockets.ConnectionClosed:
        pass
    finally:
        del clients[client.id]
        logger.info("Client %s disconnected (%d remaining).", client.id, len(clients))
        if client.role == "admin" and not _has_admin() and clients:
            successor = next(iter(clients.values()))
            successor.role = "admin"
            logger.info("Promoted client %s to admin (previous admin left).", successor.id)
        await _broadcast_state()


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    asyncio.create_task(_heartbeat())
    async with websockets.serve(_handle_client, HOST, PORT):
        logger.info("Sync radio server listening on %s:%d", HOST, PORT)
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
