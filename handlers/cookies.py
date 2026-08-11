"""Owner cookie-management command: /cookies — upload a fresh cookies.txt
so the yt-dlp downloader can use it for YouTube (fixes bot-checks)."""
from __future__ import annotations

import logging
import os

from pyrogram import filters
from pyrogram.types import Message

import config
from modules import filters as guard

logger = logging.getLogger("auramusic")

# Path where the uploaded cookies.txt lands.
COOKIES_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cookies.txt")


def register(app):
    @app.on_message(filters.command("cookies", prefixes=["/", "!"]) & filters.private)
    async def cookies_cmd(client, message: Message):
        user = message.from_user
        if not user or not guard.is_owner(user.id):
            await message.reply_text("🚫 *Access denied.* You are not authorized to use this command.")
            return

        if not message.document:
            await message.reply_text(
                "🍪 *Send cookies.txt*\n\n"
                "Send me your `cookies.txt` file (Netscape format) exported from a "
                "logged-in browser — I'll save it and use it for YouTube downloads. "
                "This fixes \"Sign in to confirm you're not a bot\" / \"The page needs "
                "to be reloaded\" errors.\n\n"
                "Export it from your browser with a cookies-export extension "
                "(e.g. \"Get cookies.txt LOCALLY\"), then send the file here."
            )
            return

        file_name = (message.document.file_name or "").lower()
        if not file_name.endswith(".txt"):
            await message.reply_text("❌ Please send a `.txt` file (cookies.txt format).")
            return

        await message.reply_text("📥 Downloading cookies.txt…")
        path = await message.download(file_name="cookies_new.txt")
        if not path:
            await message.reply_text("❌ Could not download the file. Try again.")
            return

        # validate: Netscape cookie file has tab-separated columns, contains youtube.com
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                content = fh.read()
            lines = [ln for ln in content.splitlines() if ln.strip() and not ln.strip().startswith("#")]
            if not any("youtube.com" in ln for ln in lines):
                await message.reply_text(
                    "❌ That file doesn't look like a YouTube cookies file "
                    "(no `youtube.com` entries). Export from a logged-in YouTube/browser session."
                )
                os.remove(path)
                return
        except OSError as e:
            await message.reply_text(f"❌ Could not read the file: {e}")
            return

        # replace the live cookies.txt
        os.replace(path, COOKIES_PATH)
        os.chmod(COOKIES_PATH, 0o600)
        # point config at it
        if not config.COOKIES_FILE:
            config.COOKIES_FILE = COOKIES_PATH
        logger.info("Owner uploaded new cookies.txt (%d lines)", len(lines))
        await message.reply_text(
            f"✅ *Cookies saved!*\n\n"
            f"File: `{COOKIES_PATH}`\n"
            f"Entries: {len(lines)} (includes YouTube)\n\n"
            "The downloader will use it on the next track. "
            "Restart the bot if downloads still fail."
        )
