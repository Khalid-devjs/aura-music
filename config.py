"""
Aura Music Bot — configuration.

All settings come from environment variables (.env file supported).
"""
import os
import sys

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# ---------- Telegram API (required) ----------
API_ID: int = _int("API_ID")
API_HASH: str = os.getenv("API_HASH", "").strip()
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
# Pyrogram string session of the USER account used for voice-chat streaming.
SESSION_STRING: str = os.getenv("SESSION_STRING", "").strip()

# ---------- Owner / admins ----------
OWNER_ID: int = _int("OWNER_ID")

# ---------- Database ----------
# SQLite by default (zero-config). Path can be overridden with DB_PATH.
DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip()
DB_PATH: str = os.getenv("DB_PATH", "musicbot.db")

# ---------- Player ----------
CACHE_DIR: str = os.getenv("CACHE_DIR", "cache")
MAX_QUEUE: int = _int("MAX_QUEUE", 50)
MAX_DURATION: int = _int("MAX_DURATION", 0)  # 0 = unlimited (seconds)
DEFAULT_VOLUME: int = max(0, min(200, _int("DEFAULT_VOLUME", 100)))
MAX_TRACK_SIZE_MB: int = _int("MAX_TRACK_SIZE_MB", 512)
CACHE_CLEANUP_OLDER_THAN_H: int = _int("CACHE_CLEANUP_OLDER_THAN_H", 24)
# YouTube extractor: 'tv' client dodges datacenter bot-checks; 'default' uses stock yt-dlp.
YT_CLIENT: str = os.getenv("YT_CLIENT", "web")
# Optional cookies.txt (Netscape format) exported from your browser — fixes
# "Sign in to confirm you're not a bot" when even tv/android clients are blocked.
COOKIES_FILE: str = os.getenv("COOKIES_FILE", "").strip()

# ---------- Behaviour ----------
AUTO_DELETE_MS: int = _int("AUTO_DELETE_MS", 30000)  # delete old bot msgs after N ms (0 = off)
LOG_CHANNEL_ID: int = _int("LOG_CHANNEL_ID", 0)      # 0 = disabled
RATE_LIMIT_CALLBACKS: int = _int("RATE_LIMIT_CALLBACKS", 6)   # per window
RATE_LIMIT_WINDOW_S: int = _int("RATE_LIMIT_WINDOW_S", 4)
BOT_USERNAME: str = os.getenv("BOT_USERNAME", "").strip()
SUPPORT_CHAT: str = os.getenv("SUPPORT_CHAT", "").strip()
BOT_NAME: str = os.getenv("BOT_NAME", "Aura Music")
BOT_VERSION: str = os.getenv("BOT_VERSION", "1.6.5")

# ---------- Validation ----------
def validate() -> None:
    missing = []
    if not API_ID:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not OWNER_ID:
        missing.append("OWNER_ID")
    if missing:
        print(f"[CONFIG] Missing required env vars: {', '.join(missing)}")
        print("[CONFIG] Copy .env.example to .env and fill it in.")
        sys.exit(1)
