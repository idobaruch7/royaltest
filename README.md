# RoyalTest

RoyalTest is a browser-based, private Texas Hold'em game with:

- a host screen (`/host`) for table control and game flow
- a player screen (`/join`) for mobile-friendly actions
- real-time updates powered by Flask-SocketIO
<div align="center">
  
<img width="800" height="650" alt="Screenshot_20260316_235802" src="https://github.com/user-attachments/assets/b11df972-fc81-4983-b3e7-34c141be5055" />
<img width="305" height="520" alt="Screenshot_20260316_235802" src="https://github.com/user-attachments/assets/e4e0eadd-a105-4ebf-8357-be83e830e05b" />

</div>

## Quick Start (Docker)

Pulls the pre-built image from GitHub Container Registry and runs it locally. No cloning required.

```bash
docker pull ghcr.io/idobaruch7/royaltest:nightly
docker run --rm -p 5000:5000 ghcr.io/idobaruch7/royaltest:nightly
```

Then open:

- Host: `http://localhost:5000/host`
- Join: `http://localhost:5000/join`
- Landing page: `http://localhost:5000/`

If players join from other devices on your network, use your computer's LAN IP instead of `localhost`.

## Docker Compose

Clone the repo, then build and run from source:

```bash
git clone https://github.com/idobaruch7/royaltest.git
cd royaltest
docker compose up --build
```

This builds from `Dockerfile`, starts the container, publishes port `5000`, and persists SQLite data in a named volume.

## Local Development

### 1) Create a virtual environment

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
pip install -r requirements.txt
```

### 2) Run the server

```bash
python3 server/app.py
```

Then open:

- Host: `http://localhost:5000/host`
- Join: `http://localhost:5000/join`
- Landing page: `http://localhost:5000/`

## Environment Variables

You can configure runtime behavior with environment variables:

- `ROYALTEST_SECRET_KEY`: Flask secret key (default: dev fallback)
- `ROYALTEST_HOST`: bind host (default: `0.0.0.0`)
- `ROYALTEST_PORT`: bind port (default: `5000`)
- `ROYALTEST_DEBUG`: debug mode (`1/true/yes/on` to enable, default off)

Example:

```bash
ROYALTEST_SECRET_KEY="change-me" ROYALTEST_DEBUG=1 python3 server/app.py
```

## Gameplay Flow

1. Open `/host` on the main display (a unique table code is generated).
2. Players join from their phones using `/join` and enter that table code.
3. Host starts the game when enough players are seated.
4. New joiners during a hand are queued for the next hand.

## Project Structure

- `server/app.py` - Flask + Socket.IO server and routes
- `server/game_engine.py` - poker engine and hand/state logic
- `server/bot_player.py` - bot and player decision model
- `server/db.py` - SQLite helpers
- `public/host/index.html` - host UI
- `public/player/index.html` - player UI
- `public/index.html` - landing page

## Developer Commands

### Run syntax checks

```bash
python3 -m compileall server
```

### Lint

```bash
ruff check server
```

### Type checks

```bash
mypy server
```

## Notes

- This project is designed for local/private play.
- `game.db` is intentionally ignored by git.
