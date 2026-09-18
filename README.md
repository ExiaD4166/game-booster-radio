# Game Booster & Radio Center

A lightweight Windows desktop app for gamers: a one-click game optimizer that gives
your active game priority over background apps, paired with a synchronized radio so
you and your friends can listen to the same YouTube audio together while you play —
without touching video decoding or adding noticeable CPU/RAM overhead.

**[⬇ Download the latest release](https://github.com/ExiaD4166/game-booster-radio/releases/latest)**
— no Python or VLC install needed. Extract the **whole folder**, then run `GameBoosterRadio.exe`.

![Game Booster & Radio Center](assets/screenshot.png)

## Quick start

**Boost a game**
1. Start your game, then click **Refresh**. Running programs are listed heaviest-memory-first
   (with their RAM use), so your game is usually at the top. The list has a scrollbar.
2. Pick it and click **BOOST GAME**. Click **UN-BOOST** to undo everything — or just close
   the game, and the boost is undone automatically.

**Listen together**
1. Type the server address into the *Server address* field (for example `ws://localhost:8765`
   for a local server, or `wss://your-app.onrender.com` for a hosted one) and click **Connect**.
2. The first person to connect becomes **admin**. The admin pastes a YouTube video or playlist
   link and clicks **Sync for Everyone** — everyone hears the same audio at the same moment.
3. Not connected, or no admin stream running? Paste a link and click **Play Locally** to
   listen on your own.

> Free hosts sleep when idle, so the very first connect can take 30-60 seconds.

## What it does

**Game Optimizer**
- Raises the game's priority to `HIGH_PRIORITY_CLASS` and leaves it free to use every CPU core.
- Lowers safe-to-lower background apps — browsers and chat clients drop to `Low`, other user
  apps to `Below Normal` — and lowers their disk I/O priority so they can't crowd the game's
  asset streaming. RAM is trimmed only for the known-heavy ones.
- Never touches what a game depends on: Windows components, graphics/audio drivers, input
  tools, capture/overlay software, anti-cheat, or the game's own launcher and helpers. If
  several processes share the game's name, the one using the most memory is boosted.
- Requests the finest system timer resolution while boosted, and switches to the High/Ultimate
  Performance power plan if one already exists on the PC and you're plugged in (never on battery).
- One-shot and lightweight: everything happens once when you click Boost. The only thing
  running afterward is a thread that sleeps inside Windows until the game exits, using no CPU.
- Clears temp files older than 24 hours from `AppData\Local\Temp` and `Windows\Temp`, in
  low-priority background mode (never anything newer, since running apps may still need it).
- Restores exactly what it changed. Processes are tracked by id *and* start time, so a
  recycled process id can never be mistaken for one that was boosted.
- Crash-safe: if the app ever crashes mid-boost, it detects the leftover state on the next
  launch and restores everything, including the power plan.
- Games that run as administrator can only be boosted if Game Booster is also run as
  administrator; the app tells you when that's the case.

**Sync Radio**
- Connect to a shared room (self-hosted or hosted) and listen to the same YouTube audio,
  in sync, with everyone else connected.
- Paste a single video, a playlist, or a video link that belongs to a playlist (the whole
  playlist is queued, starting at the video you pasted). Tracks advance automatically, and
  the label shows your place, e.g. `Now playing (2/23): ...`.
- One admin controls the shared stream — Pause and Next. Admins can promote other users to
  admin. While an admin stream is live, everyone else's controls are locked to it.
- Optional admin password: enter it in the *Admin password* field before connecting to be
  admin regardless of who connected first (see the server settings below).
- Playing on your own (no admin stream)? Playlists, Pause and Next all work locally, with or
  without a server connection, and never affect anyone else.
- The Radio ON/OFF toggle and volume slider are always yours alone.
- Stays in sync automatically: joining mid-track lands on the right moment, small drift is
  closed by playing very slightly faster or slower (no audible seek), and only a large gap
  triggers a jump.
- Audio-only streaming (`yt-dlp` + VLC) — no video is ever downloaded or rendered.

## Tech stack

- **Python 3.11+**
- **CustomTkinter** — the desktop GUI
- **psutil** + Windows `ctypes` APIs — process/I/O priority, timer resolution, exit detection
- **websockets** + `asyncio` — the real-time sync server and client
- **yt-dlp** + **python-vlc** — audio-only YouTube extraction and playback
- **Pillow** — custom-drawn icons and badges
- **PyInstaller** — packaging into a standalone Windows `.exe`

## Running from source

```bash
git clone <this-repo-url>
cd radio
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Running from source needs [VLC](https://www.videolan.org/vlc/) installed (the packaged `.exe`
bundles its own).

Start the sync server (needed for the shared radio; the game optimizer and personal playback
work with no server running):

```bash
python server.py
```

In another terminal, start the app:

```bash
python app.py
```

By default the app connects to `ws://localhost:8765`. To connect to a different server,
type its address into the "Server address" field before clicking Connect.

## Building the `.exe` yourself

Requires VLC installed at its default location (`C:\Program Files\VideoLAN\VLC`) on the
machine doing the build — *not* on the machines that will run the `.exe`.

```bash
pip install -r requirements-build.txt
pyinstaller GameBoosterRadio.spec
```

The finished app appears in `dist/GameBoosterRadio/`. Zip that whole folder to share it
(publish it as a GitHub Release rather than committing it to the repo).

## Deploying your own sync server

The included server is lightweight and designed to run on a free-tier host like
[Render](https://render.com):

- **Build command**: `pip install -r requirements-server.txt`
- **Start command**: `python server.py`

Settings, all optional environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `HOST` | `0.0.0.0` | Interface to listen on |
| `PORT` | `8765` | Port to listen on (hosts like Render set this for you) |
| `ADMIN_PASSWORD` | unset | If set, anyone who sends this password when connecting becomes admin immediately |

The same code runs unchanged locally and on a hosting platform — no configuration file to edit.
Hosted servers use TLS, so connect with `wss://` rather than `ws://`.

## Project structure

| File | Purpose |
|---|---|
| `app.py` | The desktop GUI — wires everything else together |
| `optimizer.py` | Game boost engine: priorities, protection rules, restore, power/timer, temp cleanup |
| `radio_player.py` | Audio-only YouTube extraction, playlist expansion and VLC playback |
| `server.py` | WebSocket sync server (roles, shared playback state, queue) |
| `sync_client.py` | WebSocket client used by the desktop app |
| `GameBoosterRadio.spec` | PyInstaller build configuration |
