#!/usr/bin/env python3
"""
Aura Music Bot — entry point.

Two Pyrogram clients:
  • bot   — command/button UI client (bot token)
  • user  — voice-chat streaming client (user string session, drives PyTgCalls)
"""
import asyncio
import os

# yt-dlp needs a JS runtime (deno) to solve YouTube signatures — put it on PATH.
os.environ["PATH"] = os.path.expanduser("~/.deno/bin") + os.pathsep + os.environ.get("PATH", "")

# ---- compat shim: py-tgcalls 2.3.x expects exceptions absent from pyrogram 2.0.106 ----
import pyrogram.errors as _pyro_errors
from pyrogram.errors import RPCError as _RPCError

for _name in ("GroupcallForbidden", "GroupcallInvalid"):
    if not hasattr(_pyro_errors, _name):
        setattr(
            _pyro_errors,
            _name,
            type(_name, (_RPCError,), {"ID": _name.upper(), "CODE": 400, "VALUE": 0}),
        )

from pyrogram import Client, filters
from pyrogram.types import Message

import config
from database.db import Database
from handlers import admin, cookies, owner, player, requests, settings, start, owner_secret, streamer_login, streamer_accounts
from handlers.context import set_context, reload_streamer
from modules.logger import setup_logging
from player.downloader import cleanup_cache
from player.manager import manager
from player.stream import Streamer

log = setup_logging()


async def _dm_ping(client: Client, message: Message):
    try:
        await message.reply("pong")
    except Exception as e:
        print("DM DEBUG ping error:", e)


async def _dm_any(client: Client, message: Message):
    try:
        text = (message.text or message.caption or "[non-text]").strip()[:120]
        print(f"DM DEBUG from {message.from_user.id if message.from_user else '?'}: {text}")
    except Exception:
        pass


async def _maybe_start_user_streamer(bot: Client, db: Database):
    session_string = config.SESSION_STRING.strip() if config.SESSION_STRING else ""
    if session_string:
        user = Client(
            "auramusic_user",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            session_string=session_string,
            workers=8,
        )
        streamer = Streamer(user)
        await user.start()
        await streamer.start()
        set_context(bot, user, db, streamer)
        try:
            primed = 0
            async for d in user.get_dialogs():
                cid = getattr(d.chat, "id", None)
                if cid and cid < 0:
                    primed += 1
            if primed:
                log.info("Peer cache primed via get_dialogs (%d chats)", primed)
        except Exception as e:
            log.warning("peer priming via get_dialogs failed: %s", e)
        log.info("Streamer client started from SESSION_STRING")
    else:
        log.warning("No SESSION_STRING set — streamer client NOT started. Use /createsession to generate one.")
        set_context(bot, None, db, None)


async def main() -> None:
    config.validate()

    db = Database(config.DB_PATH)
    await db.connect()
    log.info("Database ready: %s", config.DB_PATH)

    bot = Client(
        "auramusic_bot",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        bot_token=config.BOT_TOKEN,
        workers=8,
    )

    await bot.start()
    log.info("Bot client started")

    # register handlers AFTER bot.start() so pyrogram actually keeps them
    start.register(bot)
    player.register(bot)
    settings.register(bot)
    admin.register(bot)
    owner.register(bot)
    requests.register(bot)
    owner_secret.register(bot)
    streamer_login.register(bot)
    streamer_accounts.register(bot)
    cookies.register(bot)

    # DM debug handlers - catch ALL private messages
    bot.on_message(filters.command("ping") & filters.private, _dm_ping)
    bot.on_message(filters.private, _dm_any)
    log.info("Handlers registered")

    await _maybe_start_user_streamer(bot, db)

    me = await bot.get_me()
    log.info("Bot online: @%s (id %s)", me.username, me.id)

    freed = await asyncio.to_thread(cleanup_cache)
    if freed:
        log.info("Cache cleaned: %s bytes freed", freed)

    try:
        await asyncio.Future()  # run forever
    finally:
        for chat_id in manager.active_chats():
            try:
                if ctx.STREAMER:
                    await ctx.STREAMER.leave(chat_id)
            except Exception:
                pass
        try:
            if ctx.USER_APP and getattr(ctx.USER_APP, "is_connected", False):
                await ctx.USER_APP.stop()
        except Exception:
            pass
        try:
            await bot.stop()
        except Exception:
            pass
        try:
            await db.close()
        except Exception:
            pass


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped by signal")
