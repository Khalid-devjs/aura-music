# 🎧 Aura Music Bot

A **professional Telegram voice-chat music & video streaming bot** with a premium,
button-only interface. Streams audio and video into group voice chats from
YouTube links, song searches, direct media URLs, or Telegram files.

Built with **Python 3.12 · Pyrogram · PyTgCalls · FFmpeg · yt-dlp · SQLite (async)**.

---

## ✨ Features

- 🎵 **Music streaming** — voice-chat playback from YouTube / searches / URLs / Telegram audio
- 🎬 **Video streaming** — video in group video chats
- 📜 **Queues & playlists** — multi-track queue, pagination, loop, skip
- 🎛️ **Full button UI** — /start main menu, player controls, admin panel, owner dashboard
- 👑 **Admin panel** — add/remove admins, ban/unban, broadcast, stats, restart, shutdown
- 👥 **Group management** — leave, blacklist/whitelist, per-group streaming toggle
- 🔒 **Owner dashboard** — users/groups/active calls/system info, ban/unban, logs, git-update
- 🛡️ **Security** — owner/admin-only actions, ban system, rate limiting, token-safe logging
- 🧠 **Reliability** — auto queue advance, re-join on disconnect, file cleanup, multi-group support
- 🗄️ **Database** — users, groups, admins, bans, settings, stats, playlists (SQLite by default)

---

## 📁 Structure

```
main.py              # entry point
config.py            # env-based configuration
database/db.py       # async DB layer (SQLite)
handlers/            # start, player, admin, owner, settings, context
buttons/inline.py    # all inline keyboards
player/              # queue manager, downloader (yt-dlp), streamer (PyTgCalls)
modules/             # logger, filters, rate limit, helpers
utils/formatters.py  # formatting helpers
requirements.txt
Dockerfile
.env.example
```

---

## 🚀 Quick Start (VPS / Linux)

### 1. Requirements

- **Python 3.10 – 3.12** (py-tgcalls does **not** support 3.13+)
- **FFmpeg**: `sudo apt install ffmpeg`

### 2. Credentials

1. `API_ID` + `API_HASH` → https://my.telegram.org (app api.tools)
2. `BOT_TOKEN` → from [@BotFather](https://t.me/BotFather)
3. `SESSION_STRING` → a **user account** Pyrogram string session.
   The streaming user must be a member of your groups:

```bash
python - <<'EOF'
import asyncio
from pyrogram import Client

async def main():
    app = Client("session", api_id=API_ID, api_hash=API_HASH)
    await app.start()
    print("STRING_SESSION=", await app.export_session_string())
    await app.stop()

asyncio.run(main())
EOF
```

> 💡 Use a **fresh/second Telegram account** for streaming — your main account is
> risk-exposed if you stream copyrighted content in public groups.

4. `OWNER_ID` → your Telegram user id (get it from [@userinfobot](https://t.me/userinfobot))

### 3. Run

```bash
cp .env.example .env        # fill in your values
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

### 4. Docker

```bash
docker build -t aura-music .
docker run -d --name aura-music --env-file .env -v $(pwd)/cache:/app/cache aura-music
```

### 5. systemd (optional)

```
[Unit]
Description=Aura Music Bot
After=network.target

[Service]
WorkingDirectory=/root/musicbot
ExecStart=/root/musicbot/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## 🎮 Usage

1. Add the bot **and the streaming user account** to your group.
2. Open the group **voice chat** (or video chat).
3. Tap **🎵 Play Music** → send a song name / link / audio file. That's it.

| Button | Action |
|---|---|
| ⏸️ / ▶️ | Pause / resume |
| ⏭️ | Skip track |
| ⏹️ | Stop + clear queue |
| 🔊 | Volume (−10 / +10 / mute / unmute) |
| 📜 | Queue view (paginated) |
| 🔁 | Loop current track |

Commands also work: `/play <song>`, `/vplay <song>`, `/pause`, `/resume`,
`/skip`, `/stop`, `/loop`, `/volume 80`, `/queue`.

---

## 🛡️ Security notes

- Player controls: **admin/owner only**. Everyone may queue.
- Banned users are blocked bot-wide; blacklisted groups are auto-left.
- Callback spam is rate-limited.
- `httpx` logging is silenced — bot tokens never appear in logs.
- All secrets live in `.env` (git-ignored).

## 📜 License

For personal/community use. Respect content creators' rights and platform ToS.
