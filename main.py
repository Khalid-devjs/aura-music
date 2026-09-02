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

from pyrogram import Client

import config
from database.db import Database
from handlers import admin, cookies, owner, player, requests, settings, start, owner_secret, streamer_login, streamer_accounts
from handlers.context import set_context
from modules.logger import setup_logging
from player.downloader import cleanup_cache
from player.manager import manager
from player.stream import Streamer

log = setup_logging()


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
    user = Client(
        "auramusic_user",
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=config.SESSION_STRING,
        workers=8,
    )

    streamer = Streamer(user)
    set_context(bot, user, db, streamer)

    # forward any error to the owner's DM (throttled + deduped)
    from modules.error_reporter import install as install_error_reporter

    install_error_reporter()

    # register handlers
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

    log.info("Starting clients…")
    await user.start()
    await streamer.start()
    await bot.start()

    freed = await asyncio.to_thread(cleanup_cache)
    if freed:
        log.info("Cache cleaned: %s bytes freed", freed)

    # PRIME the userbot's peer cache. On a fresh process the peer cache is
    # empty: incoming updates from groups the userbot hasn't "met" yet fail
    # resolve_peer -> "Peer id invalid" noise. get_dialogs() forces Telegram
    # to hand us every chat the userbot is in, populating the storage cache
    # (get_chat() by raw id can itself fail resolve_peer — chicken-and-egg).
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

    me = await bot.get_me()
    log.info("Bot online: @%s (id %s)", me.username, me.id)

    try:
        await asyncio.Future()  # run forever
    finally:
        for chat_id in manager.active_chats():
            try:
                await streamer.leave(chat_id)
            except Exception:
                pass
        await bot.stop()
        await user.stop()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped by signal")
