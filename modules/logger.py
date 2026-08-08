"""
Logging setup.

CRITICAL: the `httpx` / `httpcore` loggers are silenced at INFO level because
Telegram API URLs logged there contain the FULL bot token.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "musicbot.log")


def setup_logging() -> logging.Logger:
    os.makedirs(LOG_DIR, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    fh = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)

    # ---- token-leak protection (see skill: python-telegram-bots) ----
    for noisy in ("httpx", "httpcore", "pyrogram.session", "pyrogram.connection"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # yt-dlp is chatty about its own stuff; keep info but it's fine
    logging.getLogger("yt_dlp").setLevel(logging.WARNING)
    return logging.getLogger("auramusic")
